---
scope: core
description: When a recording is documented, its original is archived out of the inbox and every derived document links back to it — both halves, never one alone
---

# Recording Provenance — archive the original, link the source

A transcript or recording that gets **documented** (summarised, evaluated,
turned into tasks) produces derived artifacts. The moment those artifacts are
written, two things are **mandatory — together, never one without the other**:

1. **Archive the original out of the inbox.** The source recording (audio +
   its naked transcript) is the immutable, recoverable original. It moves out
   of the scan inbox (`work.imports_dir`, e.g. `imports/`) into the recording
   archive (`work.audio_archive_dir` under `doc_sensor.onedrive_root` — the PARA
   `processed/` store). Before moving: **dedup by md5 + byte-size** against the
   archive — the filename carries the *push* time, never the record time, so
   name/date checks silently miss re-pushes. Then **rename descriptively** with
   the real recording time: `YYYY-MM-DD_HHMM_slug.{mp3,md}`. The original never
   lingers in the inbox.

2. **Link the source in every derived document.** Each artifact derived from the
   recording (summary, insights, evaluation, brief, protocol) carries a
   `record:` / `source:` pointer back to the archived original — so any claim
   traces to the recording in one hop. A derived document never floats without
   provenance.

## Why

Skip either half and recordings rot in the inbox while summaries float
unanchored — and the inbox reads as full of "new" work that is already done.
Concretely (2026-07-22): three already-processed recordings — two job
interviews + one cross-instance interview — sat 6–9 days in `imports/` because
the paths that documented them archived neither the audio nor a back-link. The
user could not tell processed from new.

## Where each path stands

- **`/debrief` (meeting-transcription) is the reference implementation.**
  Phase 8 already moves audio + naked transcript to the PARA archive (paired)
  and links them via the summary's `record:` frontmatter. Follow it; do not
  re-invent the mechanics.
- **Every OTHER documentation path must do the equivalent** — it has no Phase 8,
  so the invariant is on you:
  - **Application / interview evaluations** (`work/streams/applications/…`):
    move the interview audio to the archive, link it from the Auswertung/Brief.
  - **Cross-instance staging** (transcript handed to another instance's import):
    move the audio too, and keep **all siblings together** — never stage one
    part and leave the rest behind. Respect instance isolation
    (`rules/multi-instance-isolation.md`): stage into the target instance, do
    not document it in this one.
  - **Ad-hoc summaries** of any recording: same two halves.

## Backstop, not the gate

The `checkup` group `imports` (`workflow/checks/imports.yaml`, check
`no-lingering-audio`) flags audio left in the inbox > 2 days. That is the
**safety net for a missed gate**, not the gate itself — the gate is doing both
halves at documentation time, at the source.
