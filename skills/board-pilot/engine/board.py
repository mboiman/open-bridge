"""BoardClient implementations.

FakeBoardClient is the in-memory board the tests drive — it is the whole reason
the process can be proven end-to-end (Todo → PR → STOP) with zero side effects.
GhBoardClient is the real adapter over GitHub Projects v2 (filled in by the build
workflow); it is never exercised by the deterministic test suite.
"""
from __future__ import annotations

from dataclasses import replace

from .interfaces import parse_reject


class FakeBoardClient:
    """In-memory board. `prs` is the set of item ids that already have a PR."""

    def __init__(self, items, prs=None):
        self._items = {i.id: i for i in items}
        self._prs = set(prs or [])
        self._numbers: dict = {}   # (item_id, field) -> int
        self._comments: dict = {}  # item_id -> list of {id, author, text, viewerDidAuthor}
        self._next_comment_id = 1

    def fetch_items(self):
        # return copies so the engine must go through set_* (mirrors a real remote)
        out = []
        for i in self._items.values():
            item = replace(i)
            item.bounces = int(self._numbers.get((i.id, "Bounces"), 0) or 0)
            # annotation = latest ENGINE-AUTHORED reject note matching the CURRENT round
            ann = ""
            for c in self._comments.get(i.id, []):
                if c.get("author") != "bot":
                    continue  # authenticated read-back: ignore non-engine comments
                rnd, note = parse_reject(c.get("text", ""))
                if rnd == item.bounces:
                    ann = note
            item.annotation = ann
            out.append(item)
        return out

    def set_pipeline(self, item_id, value):
        self._items[item_id].pipeline = value

    def set_status(self, item_id, value):
        self._items[item_id].status = value

    def pr_exists(self, item_id):
        return item_id in self._prs

    # reject edge ----------------------------------------------------------
    def comment(self, item_id, text):
        self._comments.setdefault(item_id, []).append(
            {"id": f"IC_{self._next_comment_id}", "author": "bot", "text": text,
             "viewerDidAuthor": True}
        )
        self._next_comment_id += 1

    # comment I/O: the sticky record's sink --------------------------------
    def list_comments(self, item_id):
        """Mirrors GhBoardClient.list_comments — `{id, body, viewerDidAuthor}`, copies.

        `body` rather than the internal `text` key because that is what the real
        adapter returns from `gh issue view --json comments`, and the record layer
        subscripts it by name. The store keeps `text`/`author`: the reject read-back
        above and `comments_of()` predate this port and read those.

        Copies, like `fetch_items`: a consumer that edited a returned dict would be
        writing to the board through a READ — impossible against the real remote, so
        the divergence would only ever surface in production.
        """
        return [
            {"id": c["id"], "body": c["text"], "viewerDidAuthor": c["viewerDidAuthor"]}
            for c in self._comments.get(item_id, [])
        ]

    def edit_comment(self, comment_id, body):
        """Edit in place BY ID — never by position.

        Raises on an unknown id rather than no-op'ing, mirroring the real client: gh
        exits non-zero on an unresolvable node id, and a silent miss would let the run
        record report rows that are not on the issue.
        """
        for comments in self._comments.values():
            for c in comments:
                if c["id"] == comment_id:
                    c["text"] = body
                    return
        raise KeyError(comment_id)

    def get_number(self, item_id, field):
        return int(self._numbers.get((item_id, field), 0) or 0)

    def set_number(self, item_id, field, value):
        self._numbers[(item_id, field)] = int(value)

    # test helpers ---------------------------------------------------------
    def open_pr(self, item_id):
        self._prs.add(item_id)

    def status_of(self, item_id):
        return self._items[item_id].status

    def pipeline_of(self, item_id):
        return self._items[item_id].pipeline

    def bounces_of(self, item_id):
        return int(self._numbers.get((item_id, "Bounces"), 0) or 0)

    def comments_of(self, item_id):
        return list(self._comments.get(item_id, []))

    def add_foreign_comment(self, item_id, text):
        """A NON-engine actor posts a comment (e.g. a forged higher-round marker, or a
        body carrying the record's own sticky marker). GitHub asserts no authorship for
        it, so neither the reject read-back nor the record may ever adopt it."""
        self._comments.setdefault(item_id, []).append(
            {"id": f"IC_{self._next_comment_id}", "author": "attacker", "text": text,
             "viewerDidAuthor": False}
        )
        self._next_comment_id += 1
