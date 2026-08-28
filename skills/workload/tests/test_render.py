"""render: one declaration in, the exact bytes that belong on the machine out.

Pure by contract. Same input, same bytes, forever. The interesting assertions
here are arithmetic (the twenty minute lead) and refusals (what must never be
approximated), because both are places where a plausible wrong answer is worse
than an error.
"""

from __future__ import annotations

import plistlib
import unittest

from tests.conftest import (
    CORPUS,
    DERIVED,
    FIXTURE_HOME,
    FIXTURE_TZ,
    FIXTURE_UID,
    FakeHost,
    MachineGuard,
    SKILL_DIR,
    mod,
)

model = mod("engine.model")
errors = mod("engine.errors")
render_mod = mod("engine.render")
base = mod("engine.backends.base")
backends = mod("engine.backends")


def as_text(content) -> str:
    return content.decode("utf-8") if isinstance(content, bytes) else content


def as_bytes(content) -> bytes:
    return content if isinstance(content, bytes) else content.encode("utf-8")


class RenderBase(MachineGuard):

    def ctx(self, **overrides):
        kwargs = dict(
            uid=FIXTURE_UID,
            home=FIXTURE_HOME,
            stamp_dir=f"{FIXTURE_HOME}/.bridge/workloads",
            dispatcher_registry=None,
            host_timezone=FIXTURE_TZ,
        )
        kwargs.update(overrides)
        return base.RenderContext(**kwargs)

    def host(self, slug="host-a"):
        return FakeHost.from_fixture(slug)

    def load(self, name):
        for folder in (CORPUS, DERIVED):
            path = folder / f"{name}.yaml"
            if path.exists():
                return model.load_declaration(path)
        raise AssertionError(name)

    def render(self, name, host="host-a", **ctx_overrides):
        return render_mod.render(self.load(name), self.host(host), self.ctx(**ctx_overrides))

    def plist_of(self, artifact):
        files = [f for f in artifact.files if str(f.path).endswith(".plist")]
        self.assertEqual(len(files), 1, f"expected exactly one plist, got {[f.path for f in artifact.files]}")
        return plistlib.loads(as_bytes(files[0].content))


class TheTwentyMinuteLead(RenderBase):
    """delivery_at minus duration_estimate_min is the START.

    The declaration says the RESULT is due at 06:30 and that the run takes about
    twenty minutes, so the unit has to fire at 06:10. Migrating a job that used
    to name its start time makes the result arrive twenty minutes earlier, which
    is a behaviour change the operator has to be told about. If this test ever
    goes green on 06:30, the arithmetic was dropped and nobody would notice for
    a month.
    """

    def test_recurring_start_is_delivery_minus_duration(self):
        artifact = self.render("block-style-report")
        entries = self.plist_of(artifact)["StartCalendarInterval"]
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            self.assertEqual(entry["Hour"], 6)
            self.assertEqual(entry["Minute"], 10)
        self.assertTrue(all(e.get("Minute") != 30 for e in entries),
                        "the unit fires at the delivery time, so the lead was dropped")

    def test_every_weekday_of_the_rrule_becomes_an_entry(self):
        artifact = self.render("block-style-report")
        entries = self.plist_of(artifact)["StartCalendarInterval"]
        if isinstance(entries, dict):
            entries = [entries]
        self.assertEqual(sorted(e["Weekday"] for e in entries), [1, 2, 3, 4, 5, 6],
                         "FREQ=WEEKLY;BYDAY=MO..SA has to produce six entries, not one")

    def test_the_lead_may_cross_midnight_and_moves_the_weekday_back(self):
        # 00:10 minus 20 minutes is 23:50 on the previous day. A backend that
        # only subtracts the minutes fires the Monday job on Monday night.
        artifact = self.render("midnight-report")
        entries = self.plist_of(artifact)["StartCalendarInterval"]
        if isinstance(entries, dict):
            entries = [entries]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry["Hour"], entry["Minute"]), (23, 50))
        self.assertIn(entry["Weekday"], (0, 7),
                      "a Monday delivery starting at 23:50 starts on Sunday")

    def test_a_zero_duration_leaves_the_delivery_time_alone(self):
        artifact = self.render("foreign-timezone-report", host="host-a",
                               host_timezone="Pacific/Auckland")
        entries = self.plist_of(artifact)["StartCalendarInterval"]
        if isinstance(entries, dict):
            entries = [entries]
        self.assertEqual((entries[0]["Hour"], entries[0]["Minute"]), (9, 0))


class IntervalHasNoClockTime(RenderBase):

    def test_interval_renders_start_interval(self):
        artifact = self.render("calendar-export")
        plist = self.plist_of(artifact)
        self.assertEqual(plist["StartInterval"], 900)

    def test_interval_never_renders_an_appointment(self):
        # An interval job has no appointment: its phase is anchored at load
        # time, which nobody records. Rendering a clock time invents one.
        plist = self.plist_of(self.render("calendar-export"))
        self.assertNotIn("StartCalendarInterval", plist)
        for key in ("Hour", "Minute", "Weekday", "Day", "Month"):
            self.assertNotIn(key, plist)

    def test_run_at_load_stays_false_for_recurring_and_interval(self):
        # A bootstrap at 15:00 must not fire the 06:10 report.
        for name in ("calendar-export", "block-style-report"):
            with self.subTest(workload=name):
                plist = self.plist_of(self.render(name))
                self.assertFalse(plist.get("RunAtLoad", False))

    def test_watch_renders_paths_and_cadence_together(self):
        plist = self.plist_of(self.render("voicememo-notify"))
        self.assertEqual(list(plist["WatchPaths"]), ["/opt/bridge/transcripts/voicememo"])
        self.assertEqual(plist["StartInterval"], 120,
                         "the cadence fallback is deliberate, a watcher can fire too early")

    def test_daemon_renders_keepalive(self):
        plist = self.plist_of(self.render("voice-channel"))
        self.assertTrue(plist.get("KeepAlive"))


class Refusals(RenderBase):

    def test_owner_foreign_is_refused(self):
        with self.assertRaises(errors.NotProvisionable) as ctx:
            self.render("public-funnel")
        self.assert_error(ctx, "not-provisionable", "public-funnel")

    def test_runtime_manual_is_refused(self):
        with self.assertRaises(errors.NotProvisionable) as ctx:
            self.render("chat-channel")
        self.assert_error(ctx, "not-provisionable", "chat-channel")

    def test_the_refusal_says_why_the_declaration_exists_anyway(self):
        with self.assertRaises(errors.NotProvisionable) as ctx:
            self.render("chat-channel")
        self.assertIn("visible", str(ctx.exception).lower(),
                      "the declaration exists to make the run visible, say so")

    def test_inert_backends_can_still_be_observed(self):
        # This is how the two non provisionable runtimes stay data instead of an
        # if statement in reconcile.
        for runtime in ("manual", "external"):
            with self.subTest(runtime=runtime):
                backend = backends.get_backend(runtime)
                self.assertTrue(callable(backend.default_probe))
                self.assertTrue(callable(backend.discover_steps))

    def test_an_inexpressible_recurrence_is_refused_never_approximated(self):
        # A parser that silently understands only FREQ=DAILY turns every other
        # entry into a single fire, and the calendar looks plausible afterwards.
        with self.assertRaises(errors.UnsupportedRecurrence) as ctx:
            self.render("exotic-recurrence")
        self.assert_error(ctx, "unsupported-recurrence", "BYSETPOS")

    def test_a_foreign_timezone_is_refused_rather_than_silently_drifting(self):
        # launchd runs StartCalendarInterval in the machine's local time only.
        with self.assertRaises(errors.UnsupportedTimezone) as ctx:
            self.render("foreign-timezone-report", host_timezone="Europe/Berlin")
        self.assert_error(ctx, "unsupported-timezone", "Pacific/Auckland", "Europe/Berlin")

    def test_the_same_zone_renders_without_complaint(self):
        artifact = self.render("block-style-report", host_timezone="Europe/Berlin")
        self.assertTrue(artifact.files)

    def test_an_unsupported_runtime_for_the_platform_is_refused(self):
        with self.assertRaises(errors.UnsupportedRuntime) as ctx:
            self.render("linux-timer-report", host="host-a")
        self.assert_error(ctx, "unsupported-runtime", "systemd", "macos")

    def test_an_unsupported_kind_for_the_backend_is_refused(self):
        with self.assertRaises(errors.UnsupportedKind) as ctx:
            self.render("cron-daemon-refused", host="host-b")
        self.assert_error(ctx, "unsupported-kind", "daemon", "cron")


class GuaranteeArithmetic(RenderBase):

    def test_launchd_gets_a_wrapper_for_what_it_cannot_promise(self):
        g = model.Guarantee
        artifact = self.render("block-style-report")
        self.assertIn(g.deadline, set(artifact.guarantees_wrapped))
        self.assertIn(g.process_group_kill, set(artifact.guarantees_wrapped))
        self.assertEqual(len(artifact.files), 2,
                         "a wrapped workload has a unit AND a guard script")

    def test_systemd_needs_no_wrapper_for_the_deadline_or_the_group_kill(self):
        g = model.Guarantee
        artifact = render_mod.render(
            self.load("linux-timer-report"), self.host("host-b"), self.ctx())
        native = set(artifact.guarantees_native)
        self.assertIn(g.deadline, native)
        self.assertIn(g.process_group_kill, native)
        self.assertNotIn(g.deadline, set(artifact.guarantees_wrapped))
        self.assertNotIn(g.process_group_kill, set(artifact.guarantees_wrapped))

    def test_cron_guarantees_nothing_natively_and_is_always_wrapped(self):
        artifact = render_mod.render(
            self.load("cron-cadence"), self.host("host-b"), self.ctx())
        self.assertEqual(set(artifact.guarantees_native), set())
        self.assertTrue(set(artifact.guarantees_wrapped))

    def test_what_stays_unmet_is_recorded_in_the_notes(self):
        # Two faults at once, and either alone was enough to make this
        # decorative. `assertIsNotNone` on `notes`, which is a str and can never
        # be None; and `block-style-report`, whose unmet set is EMPTY, so the
        # branch the test is named after was never entered. Deleting the whole
        # `if unmet:` block from render._notes left it green.
        #
        # Driven from a case that really has an unanswered guarantee now, and it
        # asserts the guarantee is NAMED. A note that says something is missing
        # without saying what is the same silence with more words.
        artifact = self.render("elevated-daemon")
        covered = set(artifact.guarantees_native) | set(artifact.guarantees_wrapped)
        unmet = set(model.required_guarantees(self.load("elevated-daemon"))) - covered
        self.assertTrue(unmet,
                        "the fixture no longer has an unanswered guarantee, so this "
                        "test stopped reaching the branch it is named after")
        for guarantee in unmet:
            self.assertIn(guarantee.value, artifact.notes,
                          f"{guarantee.value} is answered by nothing and the notes "
                          f"do not say so: {artifact.notes!r}")

    def test_what_is_answered_is_not_reported_as_unmet(self):
        # The control under the case above: without it, notes that simply list
        # every guarantee would pass.
        artifact = self.render("block-style-report")
        covered = set(artifact.guarantees_native) | set(artifact.guarantees_wrapped)
        unmet = set(model.required_guarantees(self.load("block-style-report"))) - covered
        self.assertEqual(unmet, set(), "the wrong fixture: this one has to be fully covered")
        self.assertNotIn("NOT guaranteed", artifact.notes,
                         f"a fully covered artifact reported something as unmet: "
                         f"{artifact.notes!r}")


class Determinism(RenderBase):

    def test_rendering_twice_produces_the_same_bytes(self):
        a = self.render("block-style-report")
        b = self.render("block-style-report")
        self.assertEqual([as_bytes(f.content) for f in a.files],
                         [as_bytes(f.content) for f in b.files])
        self.assertEqual(a.digest, b.digest)

    def test_no_artifact_carries_a_timestamp(self):
        # A timestamp inside the bytes makes every render look like drift.
        artifact = self.render("block-style-report")
        for f in artifact.files:
            text = as_text(f.content)
            for stamp in ("2026-", "2027-", "T00:", "GMT"):
                self.assertNotIn(stamp, text, f"{f.path} carries a clock reading")

    def test_the_digest_covers_every_file_not_only_the_unit(self):
        artifact = self.render("block-style-report")
        self.assertTrue(artifact.digest.startswith("sha256:"))
        self.assertEqual(artifact.digest, base.digest_of(artifact.files))

    def test_an_edited_wrapper_changes_the_digest(self):
        artifact = self.render("block-style-report")
        files = list(artifact.files)
        guard = [f for f in files if not str(f.path).endswith(".plist")][0]
        edited = base.RenderedFile(path=guard.path, mode=guard.mode,
                                   content=as_text(guard.content) + "\n# touched\n")
        others = [f for f in files if f is not guard]
        self.assertNotEqual(artifact.digest, base.digest_of(others + [edited]))


class Markers(RenderBase):

    def test_the_launchd_marker_is_readable_back_out_of_the_unit(self):
        artifact = self.render("block-style-report")
        env = self.plist_of(artifact)["EnvironmentVariables"]
        self.assertEqual(env[model.MARKER_ENV_ID], "block-style-report")
        self.assertTrue(env[model.MARKER_ENV_DIGEST].startswith("sha256:"))

    def test_the_uid_comes_from_the_context_not_from_a_literal(self):
        artifact = self.render("block-style-report")
        self.assertIn(f"gui/{FIXTURE_UID}", artifact.unit_ref)
        joined = artifact.unit_ref + "".join(as_text(f.content) for f in artifact.files)
        self.assertNotIn("501", joined,
                         "a hardcoded uid points the whole plan at the wrong domain")

    def test_the_label_prefix_comes_from_config(self):
        artifact = self.render("block-style-report")
        self.assertIn("bridge.block-style-report", artifact.unit_ref)


class Preflight(RenderBase):

    def test_preflight_is_quiet_when_everything_fits(self):
        found = render_mod.preflight(self.load("block-style-report"), self.host("host-a"))
        self.assertEqual(list(found), [])

    def test_preflight_names_both_the_platform_and_the_runtime(self):
        found = render_mod.preflight(self.load("linux-timer-report"), self.host("host-a"))
        text = " ".join(str(f) for f in found)
        self.assertIn("systemd", text)
        self.assertIn("macos", text)

    def test_preflight_flags_a_run_that_needs_elevation(self):
        found = render_mod.preflight(self.load("elevated-daemon"), self.host("host-a"))
        self.assertTrue(found)
        self.assertIn("elevation", " ".join(str(f) for f in found).lower())

    def test_preflight_flags_an_unsatisfiable_guarantee(self):
        # The dispatcher declares no guarantees by default, and this workload
        # demands a deadline plus a process group kill.
        found = render_mod.preflight(self.load("contract-review-reminder"), self.host("host-a"))
        self.assertTrue(found)

    def test_preflight_touches_nothing(self):
        # This used to call preflight and then grep engine/render.py for the word
        # "subprocess". The work preflight does happens across the backends
        # package, which that scan never opened: a subprocess planted in
        # `wrapper.supplies()` wrote a file while this test ran, and it stayed
        # green. The doors are shut around the call itself now.
        workload, host = self.load("block-style-report"), self.host("host-a")
        found = self.assert_pure(lambda: render_mod.preflight(workload, host),
                                 what="preflight()")
        self.assertIsInstance(list(found), list)


class RenderIsTableDriven(RenderBase):
    """No branch on a platform or runtime name may live in render.py."""

    def source(self) -> str:
        return (SKILL_DIR / "engine" / "render.py").read_text(encoding="utf-8")

    def test_render_does_no_io_and_reads_no_clock(self):
        # Same scar as `test_preflight_touches_nothing`: this was a word list
        # checked against ONE file, and every backend that render dispatches into
        # sat outside it. A subprocess in `wrapper.supplies()` walked past it.
        #
        # Measured now, across the whole dispatch, for the two backends that
        # render the most and for a case that goes through the wrapper.
        for name in ("block-style-report", "linux-timer-report", "cron-cadence"):
            with self.subTest(declaration=name):
                workload = self.load(name)
                host = self.host("host-b" if name != "block-style-report" else "host-a")
                context = self.ctx()
                artifact = self.assert_pure(
                    lambda: render_mod.render(workload, host, context),
                    what=f"render({name})")
                self.assertTrue(artifact.files, "nothing was rendered at all")

        # And the property the clock guard is really about: same input, same
        # bytes, twice.
        workload, host, context = self.load("block-style-report"), self.host("host-a"), self.ctx()
        first = render_mod.render(workload, host, context)
        second = render_mod.render(workload, host, context)
        self.assertEqual([(str(f.path), f.content) for f in first.files],
                         [(str(f.path), f.content) for f in second.files])

    def test_render_names_no_backend_and_no_platform(self):
        source = self.source()
        for name in ("launchd", "systemd", "cron", "dispatcher", "macos", "linux"):
            self.assertNotIn(f'"{name}"', source, f"render compares against {name!r}")
            self.assertNotIn(f"'{name}'", source, f"render compares against {name!r}")

    def test_the_registry_is_the_only_lookup(self):
        registry = backends.BACKENDS
        self.assertEqual(
            set(registry),
            {"launchd", "launchd-system", "systemd", "cron", "dispatcher", "manual", "external"},
        )

    def test_every_backend_declares_its_capabilities_as_data(self):
        for name, backend in backends.BACKENDS.items():
            with self.subTest(backend=name):
                self.assertEqual(backend.name, name)
                self.assertIsInstance(backend.platforms, frozenset)
                self.assertIsInstance(backend.kinds, frozenset)
                self.assertIsInstance(backend.guarantees, frozenset)

    def test_an_unknown_runtime_is_refused_by_the_registry(self):
        with self.assertRaises(errors.UnknownBackend) as ctx:
            backends.get_backend("podman")
        self.assert_error(ctx, "unknown-backend", "podman")


if __name__ == "__main__":
    unittest.main()


class TheGuardPathReachesTheToolsThatAreActuallyInstalled(RenderBase):
    """A PATH without the package manager prefix empties a report in silence.

    The guard names a PATH because a service manager gives none, which is
    right. The one it named was `/usr/bin:/bin:/usr/sbin:/sbin`, and on macOS
    almost nothing an operator installs lives there.

    Measured on a real machine while migrating the first job: under that PATH
    `gh` does not resolve at all, and `python3` resolves to the system one,
    which carries none of the third party modules. The job it was replacing had
    carried the package manager prefixes in its own unit file all along, so the
    migration would have swapped a working report for an empty one, exit code
    zero, no error, and the only symptom a shorter email.

    Both prefixes are named, not one: `/opt/homebrew/bin` on Apple Silicon and
    `/usr/local/bin` on Intel and on most Linux boxes. A directory that does not
    exist costs a lookup and nothing else, so this needs no platform branch.
    """

    def guard_text(self, name="calendar-export"):
        artifact = self.render(name)
        for f in artifact.files:
            if str(f.path).endswith(".guard.sh"):
                return f.content if isinstance(f.content, str) else f.content.decode()
        self.fail("the artifact carries no guard script")

    def path_line(self, text):
        for line in text.splitlines():
            if line.startswith("PATH="):
                return line[len("PATH="):]
        self.fail("the guard names no PATH at all, which is the older defect")

    def test_the_package_manager_prefixes_come_first(self):
        entries = self.path_line(self.guard_text()).split(":")
        for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
            with self.subTest(prefix=prefix):
                self.assertIn(prefix, entries,
                              f"{prefix} is where an installed tool actually is")
                self.assertLess(
                    entries.index(prefix), entries.index("/usr/bin"),
                    "behind /usr/bin it changes nothing for a name the system "
                    "also ships, which is the case that matters: python3")

    def test_the_system_directories_are_still_there(self):
        entries = self.path_line(self.guard_text()).split(":")
        for required in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(required, entries,
                          "the guard still has to reach the base system")
