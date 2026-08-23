# Council tools

Version-controlled source for the local council forecast ledger validator and descriptive Brier
reporter. Runtime logs and resolution evidence are private state and are never committed here.

The governing pre-implementation design is in
`design/2026-08-22-forecast-scoring-mvp.md`.

## Verification and activation

Run the isolated suite and copied-runtime rehearsal before installation:

```sh
PYTHONPATH=src:. python3 -m unittest \
  tests.test_forecasts tests.test_cli tests.test_install tests.test_legacy_report \
  tests.test_rehearse -v
python3 rehearse.py --root /home/trader
python3 install.py check --root /home/trader
```

`rehearse.py` copies the four integration targets and live ledger to a temporary root. It installs
only there, runs the runtime contract tests and reporter, compares the blind-seat decision tally,
and verifies that source hashes did not change. It also runs the isolated core suite between hashes
of the live ledger, optional resolution sidecar, and all four integration targets. Every write-path
test receives an explicit temporary ledger; the staged T&R compatibility test receives explicit
temporary `PANEL_LOG` and `PANEL_RESOLVED` paths.

After council approval, install with a complete backup:

```sh
python3 install.py install --root /home/trader \
  --backup-root /home/trader/.local/state/council-tools/runtime-backups
```

The command prints the exact backup path. To roll back, verify and restore that manifest while also
backing up the pre-restore state:

```sh
python3 install.py restore --root /home/trader --backup <printed-backup-path> \
  --backup-root /home/trader/.local/state/council-tools/runtime-backups
```

The external backup root also maintains a `LATEST` pointer, so loss or cleanup of the source tree
does not erase either the backup manifest or the path needed to restore it. The installer refuses a
backup root inside `/home/trader/council-tools`. Restore intentionally preserves the forward-safe
`council-attempt` allowlist in the blind-seat tally, because attempt rows appended after activation
are permanent even when the other runtime targets roll back.

Live installation requires a clean source commit. The rendered runtime shim pins that commit and a
SHA-256 over `src/council_tools/*.py`; every invocation refuses to run if either the checked-out
commit or imported source digest drifts. `install.py check` renders and compares the same pin.

Live ledger writes are host-guarded to `manny`. A corrupt trailing JSONL write is never skipped;
after inspecting the exact line, quarantine and remove only that final line with:

```sh
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  repair-tail --path <exact-ledger-path> --confirm-final-line <line-number> \
  --backup-dir <quarantine-directory>
```

`--today` exists only for isolated tests and copied-ledger rehearsal; the CLI rejects it on live
council paths. A host migration requires a versioned authority change, rehearsal, and new council
review before writes move away from `manny`.

The existing T&R wrapper is deliberately separate: when both `PANEL_LOG` and `PANEL_RESOLVED` are
set, the pinned runtime dispatches to a version-controlled legacy reporter for that store. It does
not feed T&R records or timestamp/index resolutions into the council scorer.
