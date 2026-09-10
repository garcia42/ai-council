# Council routing after usefulness-capture retirement

V2 usefulness capture was retired by principal decision on 2026-09-10. Both
activated studies are historical evidence. Preserve their ledgers, sidecars,
artifacts, controls, activation records, maintenance-cycle receipts and failed
namespaces read-only. Do not resume either maintenance service, issue a new V2
attempt, repair or backfill a V2 run, or treat a later V1 Council as V2 data.

New Councils use the original V1 workflow against `--study council-legacy`:

```text
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  --study council-legacy attempt ...
python3 /home/trader/.claude/knowledge/council-eval/predictions_report.py \
  --study council-legacy complete ...
```

Continue to run the ordinary V1 report and blind-seat tally before and after a
Council. Historical outcome resolutions remain allowed in the sidecar belonging
to the study that issued the outcome. Do not run `study-operations-report` as a
future-Council gate: it remains useful only as a historical view of the two V2
studies, including their final unhealthy or incomplete state.

The legacy file is a mixed append-only ledger. Ordinary V1 reporting reads the
whole file, including new post-retirement Councils. Historical V2 reporting is
cryptographically bound to the retirement-time byte prefix and ignores later V1
appends. A missing, truncated, non-line-aligned or digest-mismatched prefix fails
closed; later V1 rows cannot silently enter the ended V2 denominator.

The retirement authority and exact activation IDs are recorded outside both
evidence roots at `/var/lib/ai-council-evidence/CAPTURE_RETIRED.json`. Both
maintenance services are condition-fenced by that marker and both timers remain
disabled. Removing the marker, drop-ins, or routing is a new activation decision;
it requires explicit principal authority and a new reviewed activation plan.
