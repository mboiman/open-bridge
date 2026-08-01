"""board-pilot engine — a generic, board-driven implementation pipeline.

The engine knows nothing about any specific project (a compiled product, a docs repo, …). A project
supplies a `pipeline:` config; a BoardClient supplies board I/O; a StageRunner
executes each stage's handler. Tests inject Fake* implementations so the whole
process — Todo trigger → stages → pull request → STOP — runs deterministically
with zero side effects.
"""

__version__ = "0.1.0"
