"""Real BoardClient adapter over GitHub Projects v2 (the `gh` CLI).

This is the production adapter — it talks to a live GitHub Project board via the
`gh` CLI (`gh project item-list`, `gh project item-edit`, and `gh api graphql`
for option-id resolution) plus `gh pr list` for PR existence. It is NEVER
exercised by the deterministic test suite: the engine is proven end-to-end
against `FakeBoardClient` in board.py. This file carries the side-effecting,
network-bound half that only runs against a real org/board.

Two single-select fields matter:
  * `status_field`   — HUMAN-owned trigger column (a person drags a card to
                       `Todo`); the engine only READS it to arm an item.
  * `pipeline_field` — ENGINE-owned program counter (the durable state machine
                       position). The engine writes it; humans should not touch it.

Option ids for single-selects are resolved LIVE via `gh project field-list`
(never hardcoded — they differ per board and rotate when options are edited).

Dependency-light by design: stdlib `subprocess` + `json` only. PyYAML and other
heavy deps stay out of this module.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Optional

from .interfaces import BoardItem

# The item-list page size, and the truncation tripwire that keeps it honest.
# GraphQL point cost is charged for the REQUESTED page size, not the returned
# rows — a -L 1000 read of a 6-item board pays 10x on every poll, which is how
# a 60s tick burned through an installation's rate limit overnight. 100 rows is
# far above any board this engine babysits; a board that actually fills the page
# fails LOUD below rather than being processed truncated.
_ITEM_LIST_LIMIT = 100

# `gh` has no structured error channel — rate limiting, server-side failures and
# network-down conditions arrive only as prose on stderr. Matched shapes:
#   "GraphQL: API rate limit exceeded for installation ID ..."
#   "HTTP 403: API rate limit exceeded for ..."    (REST spelling)
#   "HTTP 502: Server Error"                       (any 5xx)
#   "error connecting to api.github.com"           (gh's own offline wrapper)
#   "dial tcp ...: connect: network is unreachable" (Go net errors passed through)
#   "... dial tcp: i/o timeout"
#   "dial tcp: lookup api.github.com: no such host"
#   "Could not resolve host: api.github.com"       (curl-style resolver failure)
# The list stays TIGHT on purpose: an unknown shape defaults to NON-transient
# (fail-closed, stays loud) — a widened match would hide real defects behind
# quiet skip notes forever.
_TRANSIENT_STDERR = re.compile(
    r"rate limit"
    r"|HTTP 5\d\d"
    r"|error connecting to"
    r"|dial tcp"
    r"|i/o timeout"
    r"|could not resolve host"
    r"|lookup .* no such host",
    re.IGNORECASE,
)


class GhCliError(RuntimeError):
    """A `gh` invocation exited non-zero — carries argv + stderr for diagnosis."""

    def __init__(self, argv: "list[str]", returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"gh failed ({returncode}): {' '.join(argv)}\n{stderr.strip()}")

    def is_transient(self) -> bool:
        """True when the failure is the API's, not the operator's.

        A rate limit, a 5xx or a dead network clears on its own — the caller
        may skip and let the next poll retry. Everything else (403 bad scope,
        404, auth) never heals by waiting and must stay loud; defaulting
        unknown shapes to permanent keeps this fail-closed.
        """
        return bool(_TRANSIENT_STDERR.search(self.stderr or ""))


class BoardTruncated(RuntimeError):
    """`item-list` filled the requested page — the board view MAY be partial.

    PERMANENT until a human raises `_ITEM_LIST_LIMIT` or shrinks the board:
    every future fetch fails identically. Dedicated class so the engine's fetch
    guard can never mistake it for a transient skip — a quietly skipped
    truncation is a forever-wedge (nothing arms, nothing advances, launchd sees
    a healthy exit-0 unit and only the stdout ledger knows).
    """


# `updateIssueComment(input:{id:ID!, body:String!})` — checked against the live
# GraphQL schema rather than recalled. The `id` is the node id that
# `gh issue view --json comments` already returns, so the sticky is edited with the
# same handle it was found by; a second lookup could resolve to a different comment.
_UPDATE_COMMENT_MUTATION = (
    "mutation($id:ID!,$body:String!)"
    "{updateIssueComment(input:{id:$id,body:$body}){issueComment{id}}}"
)


class GhBoardClient:
    """BoardClient over a GitHub Projects v2 board via the `gh` CLI.

    Parameters
    ----------
    project_number : int
        The Projects v2 number (e.g. 18), as in `gh project view <number>`.
    owner : str
        The project owner — an org or user login (e.g. "your-org").
    status_field : str
        Display name of the human-owned single-select Status field.
    pipeline_field : str
        Display name of the engine-owned single-select Pipeline field.
    repo : str
        "owner/name" of the repo whose PRs/branches back the items — used by
        `pr_exists` for the `gh pr list --head bridge/<project>/<item_id>` lookup.
    branch_template : str
        The SAME per-item branch template the stage runner uses
        ("bridge/{project}/{item_id}"). `pr_exists` MUST render the head branch
        from this template (not from the project number) or the idempotency
        check looks at the wrong head and a re-tick opens a duplicate PR.
    project : str
        Project slug, the `{project}` field of ``branch_template`` — must match
        the slug the runner renders, so board and runner agree on the head branch.
    """

    def __init__(
        self,
        project_number: int,
        owner: str,
        status_field: str,
        pipeline_field: str,
        repo: str,
        branch_template: str = "bridge/{project}/{item_id}",
        project: str = "",
        bounce_field: str = "Bounces",
        reject_edge: bool = False,
    ):
        self.project_number = int(project_number)
        self.owner = owner
        self.status_field = status_field
        self.pipeline_field = pipeline_field
        self.repo = repo
        self.branch_template = branch_template
        self.project_slug = project
        # reject-edge wiring (only active when the pipeline declares a reject edge)
        self.bounce_field = bounce_field
        self.reject_edge = reject_edge
        # caches, populated lazily from the live board
        self._project_id: Optional[str] = None
        self._field_cache: dict = {}  # field_name -> {"id": str, "options": {opt_name: opt_id}}
        self._fields_raw: Optional[list] = None  # the ONE field-list read per process
        self._issue_number_by_item: dict = {}  # item id -> backing issue number

    # -- low-level gh ------------------------------------------------------
    @staticmethod
    def _run(argv: "list[str]") -> str:
        """Run a `gh` argv (shell=False) and return stdout; raise on non-zero."""
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            shell=False,
        )
        if proc.returncode != 0:
            raise GhCliError(argv, proc.returncode, proc.stderr)
        return proc.stdout

    def _run_json(self, argv: "list[str]"):
        out = self._run(argv).strip()
        return json.loads(out) if out else None

    # -- live field/option resolution -------------------------------------
    def _field_list(self) -> list:
        """All fields from ONE `gh project field-list` read, cached per instance.

        A tick process resolves fields several times (two preflights + option
        resolution); the list is one payload, so caching per FIELD still paid one
        full read per distinct name — needless rate-limit spend on every poll.
        One instance = one tick process, so the cache can never go stale mid-use.
        """
        if self._fields_raw is None:
            data = self._run_json(
                [
                    "gh",
                    "project",
                    "field-list",
                    str(self.project_number),
                    "--owner",
                    self.owner,
                    "--format",
                    "json",
                    "-L",
                    "100",
                ]
            )
            self._fields_raw = (
                ((data or {}).get("fields") or []) if isinstance(data, dict) else (data or [])
            )
        return self._fields_raw or []

    def _field(self, name: str) -> dict:
        """Resolve a single-select field's id + option-name→option-id map LIVE.

        Reads from the per-instance field-list cache, never hardcodes option ids.
        """
        if name in self._field_cache:
            return self._field_cache[name]
        for f in self._field_list():
            if f.get("name") == name:
                options = {o.get("name"): o.get("id") for o in (f.get("options") or [])}
                entry = {"id": f.get("id"), "options": options}
                self._field_cache[name] = entry
                return entry
        raise KeyError(f"field {name!r} not found on project {self.project_number} ({self.owner})")

    def _option_id(self, field_name: str, option_value: str) -> str:
        field = self._field(field_name)
        opt = field["options"].get(option_value)
        if opt is None:
            raise KeyError(
                f"option {option_value!r} not found on field {field_name!r}; "
                f"have {sorted(field['options'])}"
            )
        return opt

    def _project_node_id(self) -> str:
        """The Projects v2 node id (PVT_...), needed by `gh project item-edit`."""
        if self._project_id is None:
            data = self._run_json(
                [
                    "gh",
                    "project",
                    "view",
                    str(self.project_number),
                    "--owner",
                    self.owner,
                    "--format",
                    "json",
                ]
            )
            self._project_id = (data or {}).get("id")
        if not self._project_id:
            raise RuntimeError(
                f"could not resolve Projects v2 node id for {self.owner} #{self.project_number}"
            )
        return self._project_id

    # -- BoardClient protocol ---------------------------------------------
    def _item_rows(self) -> list:
        """Raw `gh project item-list` rows (shared by fetch_items + get_number)."""
        data = self._run_json(
            [
                "gh",
                "project",
                "item-list",
                str(self.project_number),
                "--owner",
                self.owner,
                "--format",
                "json",
                "-L",
                str(_ITEM_LIST_LIMIT),
            ]
        )
        rows = (data or {}).get("items", []) if isinstance(data, dict) else (data or [])
        # A full page means the board MAY be truncated: items beyond it would
        # silently never arm, never advance, never park. Every consumer of this
        # read makes decisions over the WHOLE board, so a possibly-partial view
        # must raise — and as the DEDICATED BoardTruncated, which the engine's
        # fetch guard treats as permanent and lets propagate (a plain skip note
        # would hide a board that can never work behind an exit-0 ledger).
        if len(rows) >= _ITEM_LIST_LIMIT:
            raise BoardTruncated(
                f"item-list returned {len(rows)} rows == the requested page size "
                f"({_ITEM_LIST_LIMIT}) on project #{self.project_number} ({self.owner}) "
                f"— the board may be truncated; refusing to process a partial view. "
                f"Raise _ITEM_LIST_LIMIT (engine/gh_board.py) if the board is really this big."
            )
        return rows

    def fetch_items(self) -> "list[BoardItem]":
        """List board items with their Status (human) + Pipeline (engine) fields."""
        rows = self._item_rows()
        items: "list[BoardItem]" = []
        for row in rows:
            # `gh project item-list --format json` flattens single-selects into
            # lower-cased field-name keys; fall back to content fields otherwise.
            status = self._field_value(row, self.status_field)
            pipeline = self._field_value(row, self.pipeline_field)
            content = row.get("content") or {}
            item_id = str(row.get("id", ""))
            number = content.get("number") or row.get("number")
            if number is not None:
                self._issue_number_by_item[item_id] = number
            item = BoardItem(
                id=item_id,
                title=row.get("title") or content.get("title", ""),
                status=status or "",
                pipeline=pipeline or None,
                url=content.get("url", "") or row.get("url", ""),
                # None = a draft card. The ARM gate reads this off the item, so the
                # number has to travel ON it — not only into the private lookup that
                # `comment()` uses.
                issue_number=number,
                # The story, FREE: `item-list --format json` already carries
                # `content.body` on every content type (Issue, PullRequest,
                # DraftIssue) — verified against a live board, not assumed. A
                # per-item `gh issue view` would multiply this poll's API cost by
                # the board size and add a failure mode to a loop that runs on a
                # timer. `or ""` because a body-less issue is a normal shape and a
                # None here would TypeError deep in the runner's `.encode()`.
                body=content.get("body") or "",
            )
            # Reject-edge read-back is GATED to pipelines that declare the edge so
            # we never pay the extra comment fetch (or risk the Number-field parse)
            # on boards that don't use it.
            if self.reject_edge:
                item.bounces = int(self._number_value(row, self.bounce_field) or 0)
                # Second gate, per item: bounces == 0 means no reject round has
                # ever landed, so there is no note to read back — and the fetch is
                # one `gh issue view` PER ITEM PER poll. The Number field already
                # rode in on this row, so the gate costs nothing. Lives HERE, not
                # only inside the helper's round guard, so the hot path stays
                # quiet structurally rather than by a callee's internals.
                item.annotation = (
                    self._latest_reject_note(number, item.bounces) if item.bounces > 0 else ""
                )
            items.append(item)
        return items

    @staticmethod
    def _field_value(row: dict, field_name: str) -> Optional[str]:
        """Pull a single-select value out of a flattened item-list row.

        gh lower-cases the field display-name into the JSON key (e.g.
        "Status" -> "status", "Pipeline" -> "pipeline"). Try a few shapes.
        """
        key = field_name.lower().replace(" ", "")
        for candidate in (field_name, field_name.lower(), key):
            if candidate in row and row[candidate] not in (None, ""):
                val = row[candidate]
                return val if isinstance(val, str) else val.get("name")
        return None

    @staticmethod
    def _number_value(row: dict, field_name: str) -> Optional[int]:
        """Pull a Number-field value out of a flattened item-list row as an int.

        DEDICATED reader — never routes a numeric through `_field_value`, whose
        `val.get("name")` raises AttributeError on a float/int and (inside the poll
        loop) wedges the whole poll. Handles the flat numeric, a nested
        ``{"number": N}`` shape, and numeric strings; returns None when absent.
        """
        key = field_name.lower().replace(" ", "")
        for candidate in (field_name, field_name.lower(), key):
            if candidate in row and row[candidate] not in (None, ""):
                val = row[candidate]
                if isinstance(val, bool):  # guard: bool is an int subclass
                    return None
                if isinstance(val, (int, float)):
                    return int(val)
                if isinstance(val, dict):
                    num = val.get("number")
                    return int(num) if isinstance(num, (int, float)) and not isinstance(num, bool) else None
                try:
                    return int(float(val))
                except (TypeError, ValueError):
                    return None
        return None

    def set_pipeline(self, item_id: str, value: str) -> None:
        self._set_single_select(item_id, self.pipeline_field, value)

    def set_status(self, item_id: str, value: str) -> None:
        self._set_single_select(item_id, self.status_field, value)

    def _set_single_select(self, item_id: str, field_name: str, value: str) -> None:
        field = self._field(field_name)
        option_id = self._option_id(field_name, value)
        self._run(
            [
                "gh",
                "project",
                "item-edit",
                "--id",
                item_id,
                "--project-id",
                self._project_node_id(),
                "--field-id",
                field["id"],
                "--single-select-option-id",
                option_id,
            ]
        )

    def branch_for(self, item_id: str) -> str:
        """Render the per-item head branch — the SAME template the runner uses.

        Board and runner MUST agree on this string; otherwise `pr_exists` checks a
        different head than the PR was opened on and the idempotency guard fails.
        """
        try:
            return self.branch_template.format(project=self.project_slug, item_id=item_id)
        except (KeyError, IndexError):
            return f"bridge/{self.project_slug or self.project_number}/{item_id}"

    # -- startup preflight: every writable value must be a live option ----
    def preflight_options(self, pipeline_values, status_values=()) -> None:
        """Fail LOUD at startup if any value the engine can WRITE is not live.

        `_option_id` raises KeyError on a value the board does not carry. That raise
        happens inside the per-item dispatch loop, where the engine's outer guard
        swallows it into a skip — so the item never advances, and the expensive LLM
        stage re-dispatches on every poll, forever, for unbounded paid spend. The
        board is hand-configured (option ids cannot be created via YAML), so a
        missing option is the EXPECTED first-run state, not an exotic one. One
        field-list read at startup converts that silent burn into a startup error
        that names the field and the value.
        """
        for field_name, values in (
            (self.pipeline_field, pipeline_values),
            (self.status_field, status_values),
        ):
            # dedupe, preserve order, drop empties: a pipeline that never writes a
            # field must not force a field-list read for it.
            wanted = [v for v in dict.fromkeys(values or ()) if v]
            if not wanted:
                continue
            try:
                field = self._field(field_name)
            except KeyError as e:
                raise RuntimeError(
                    f"board-pilot requires a single-select field {field_name!r} on project "
                    f"#{self.project_number} ({self.owner}), but it does not exist on the board. "
                    f"Add it (GitHub Project → Settings → + field → Single select, named "
                    f"{field_name!r}) with the options the pipeline writes: "
                    f"{', '.join(repr(v) for v in wanted)}."
                ) from e
            live = field.get("options") or {}
            missing = [v for v in wanted if v not in live]
            if missing:
                raise RuntimeError(
                    f"board-pilot writes {len(missing)} value(s) to field {field_name!r} on project "
                    f"#{self.project_number} ({self.owner}) that are not live options: "
                    f"{', '.join(repr(v) for v in missing)}. "
                    f"Live options: {', '.join(repr(v) for v in sorted(live))}. "
                    f"Add the missing option(s) to the field, or correct the value in the project "
                    f"YAML. Without them the write KeyErrors inside the dispatch loop, the item "
                    f"never advances, and the stage re-dispatches on every poll."
                )

    # -- reject edge: durable bounce counter + annotation sink ------------
    def preflight_reject_field(self, field_name: Optional[str] = None) -> None:
        """Fail LOUD at startup if the durable bounce Number field is absent.

        The bounce counter is the ONLY per-item terminator for the reject edge: if
        the operator never hand-added the Number field to the GitHub Project, every
        `set_number` inside the reject branch would `KeyError` (swallowed by the
        engine's per-item dispatch guard → skip-and-retry), so the costly LLM review
        would re-run on every poll forever — unbounded paid spend. A one-shot
        `field-list` preflight turns that silent failure into a loud startup error.
        """
        name = field_name or self.bounce_field
        try:
            self._field(name)
        except KeyError as e:
            raise RuntimeError(
                f"board-pilot reject edge requires a Number field {name!r} on project "
                f"#{self.project_number} ({self.owner}), but it does not exist on the board. "
                f"Add it (GitHub Project → Settings → + field → Number, named {name!r}) and retry. "
                f"Without it the durable bounce counter cannot terminate the review loop."
            ) from e

    def get_number(self, item_id: str, field: str) -> int:
        """Read a board Number field for one item (via the dedicated reader)."""
        for row in self._item_rows():
            if str(row.get("id", "")) == item_id:
                return int(self._number_value(row, field) or 0)
        return 0

    def set_number(self, item_id: str, field: str, value: int) -> None:
        """Set a board Number field — `gh project item-edit --number`, NOT a single-select."""
        field_def = self._field(field)
        self._run(
            [
                "gh",
                "project",
                "item-edit",
                "--id",
                item_id,
                "--project-id",
                self._project_node_id(),
                "--field-id",
                field_def["id"],
                "--number",
                str(value),
            ]
        )

    def _issue_number(self, item_id: str):
        """Resolve the backing issue number for a project item (cached from fetch)."""
        if item_id in self._issue_number_by_item:
            return self._issue_number_by_item[item_id]
        # not seen yet → refresh the row cache once
        self._item_rows_into_cache()
        return self._issue_number_by_item.get(item_id)

    def _item_rows_into_cache(self) -> None:
        for row in self._item_rows():
            content = row.get("content") or {}
            number = content.get("number") or row.get("number")
            if number is not None:
                self._issue_number_by_item[str(row.get("id", ""))] = number

    def comment(self, item_id: str, text: str) -> None:
        """Post an issue comment — body over STDIN (`--body-file -`), shell=False.

        The reviewer text NEVER reaches a command line; a `; rm -rf ~` body is inert.
        """
        number = self._issue_number(item_id)
        if number is None:
            raise RuntimeError(f"cannot comment: no backing issue number for item {item_id!r}")
        argv = ["gh", "issue", "comment", str(number), "--repo", self.repo, "--body-file", "-"]
        proc = subprocess.run(
            argv,
            input=text,
            capture_output=True,
            text=True,
            shell=False,
        )
        if proc.returncode != 0:
            raise GhCliError(argv, proc.returncode, proc.stderr)

    # -- comment I/O: the sticky record's sink ----------------------------
    def list_comments(self, item_id: str) -> "list[dict]":
        """The issue's comments as `{id, body, viewerDidAuthor}` — the record's sink.

        Same call `_latest_reject_note` makes: `gh issue view --json comments` already
        carries the node id, the body and GitHub's server-side authorship assertion on
        every comment, so finding the sticky costs no extra request.

        `viewerDidAuthor` is passed through RAW — absent stays absent, never defaulted.
        The record's predicate is `is not True`, and defaulting here would invent an
        authorship assertion GitHub never made, which is the fail-OPEN the predicate
        exists to prevent.

        Projected to exactly the three keys the record subscripts. A wider passthrough
        would be a shape the in-memory Fake cannot honestly mirror — and the Fake is
        what the suite actually proves the engine against.

        Errors PROPAGATE here, unlike `_latest_reject_note`, which fail-closes to "":
        a swallowed `[]` reads as "no sticky yet", so the recorder would post a FRESH
        comment — mailing every subscriber — on every event of every tick while the
        real sticky sits there unfound. `emit_guarded` at the call site is what keeps
        the raise off the termination path.
        """
        number = self._issue_number(item_id)
        if number is None:
            raise RuntimeError(f"cannot list comments: no backing issue number for item {item_id!r}")
        data = self._run_json(
            ["gh", "issue", "view", str(number), "--repo", self.repo, "--json", "comments"]
        )
        rows = (data or {}).get("comments", []) if isinstance(data, dict) else []
        return [
            {
                "id": c.get("id"),
                "body": c.get("body") or "",
                "viewerDidAuthor": c.get("viewerDidAuthor"),
            }
            for c in rows
        ]

    def edit_comment(self, comment_id: str, body: str) -> None:
        """Edit one comment in place — body over STDIN (`-F body=@-`), shell=False.

        The body is agent-authored, unbounded and may contain any byte, so it never
        reaches a command line — the same discipline `comment()` follows with
        `--body-file -`.

        `-F` is required for the `@-` stdin read, and is safe for exactly that reason:
        the `@` path short-circuits gh's magic type conversion, so a body of literally
        `123` or `true` stays a JSON string instead of becoming a number/bool and
        failing the `String!` variable. The id rides `-f` (raw) — `-F` WOULD
        magic-convert an id that happened to look numeric.

        NOT `gh issue comment --edit-last`: the engine authors two comment streams on
        one issue, and after a reject the last self-authored comment is that round's
        reject note. `--edit-last` would overwrite the note the producer reads back
        with the run record, silently — an edit notifies no one.
        """
        argv = [
            "gh", "api", "graphql",
            "-f", f"query={_UPDATE_COMMENT_MUTATION}",
            "-f", f"id={comment_id}",
            "-F", "body=@-",
        ]
        proc = subprocess.run(
            argv,
            input=body,
            capture_output=True,
            text=True,
            shell=False,
        )
        if proc.returncode != 0:
            raise GhCliError(argv, proc.returncode, proc.stderr)

    def _latest_reject_note(self, issue_number, round_n: int) -> str:
        """Latest ENGINE-AUTHORED reject note matching the CURRENT round.

        Authorship comes from `viewerDidAuthor` — computed server-side by GitHub per
        comment, already present in `gh issue view --json comments`, and identical
        under an App token, a PAT or ambient auth. Both alternatives are worse in a
        way that is silent: resolving our own login via `gh api user` (what this used
        to do) 403s under a GitHub App INSTALLATION token, which fail-closed the note
        to "" forever; and a login pinned in config is byte-identical to "the reviewer
        had nothing to say" the moment it is mistyped, also forever, also with no error.
        There is now no login string that can be wrong.

        FAIL-CLOSED by construction: the predicate is `is not True`, so a comment whose
        authorship GitHub did not positively assert (key absent, partial payload, shape
        drift) is never trusted — an attacker cannot forge a round marker to steer the
        autonomous producer. Any I/O failure also yields an empty note (never wedges
        the poll).
        """
        if not issue_number or round_n <= 0:
            return ""
        from .interfaces import parse_reject

        try:
            data = self._run_json(
                ["gh", "issue", "view", str(issue_number), "--repo", self.repo, "--json", "comments"]
            )
        except (GhCliError, json.JSONDecodeError):
            return ""
        note = ""
        for c in (data or {}).get("comments", []) if isinstance(data, dict) else []:
            if c.get("viewerDidAuthor") is not True:  # unasserted or foreign → never trusted
                continue
            rnd, body = parse_reject(c.get("body", ""))
            if rnd == round_n:
                note = body
        return note

    def pr_exists(self, item_id: str) -> bool:
        """True iff an open PR exists on the item's head branch (template-rendered)."""
        head = self.branch_for(item_id)
        out = self._run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                self.repo,
                "--head",
                head,
                "--state",
                "open",
                "--json",
                "number",
            ]
        ).strip()
        try:
            data = json.loads(out) if out else []
        except json.JSONDecodeError:
            return False
        return bool(data)
