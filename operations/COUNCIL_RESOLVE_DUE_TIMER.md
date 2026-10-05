# resolve-due on a timer (staged, not installed)

`council-resolve-due.service` and `council-resolve-due.timer` run
`resolve-due --apply` once a day, at 00:40 America/New_York, just after each
resolution day ends. Before this, outcomes were graded only when someone ran
step 1 of a council, so a quiet week left every machine-checkable outcome
ungraded.

Installing a timer is a principal-only activation. Nothing in this repository
installs these files.

## Preconditions

- The runtime is activated at a commit that contains this file. The service
  calls the installed shim, and that shim refuses to run if its pin does not
  match.
- The host is `manny`. The unit also carries `ConditionHost=manny`, because
  manny is the only host allowed to write the ledger and its sidecar.
- The on-duty failure template `onduty-failed@.service` is installed as a user
  unit (it is the service's `OnFailure=` target; verified present 2026-10-05).
- Lingering is enabled for `trader` (`loginctl show-user trader -p Linger`), so
  user timers run without a login session. This was verified on 2026-10-04.

## Install (user units, run as trader on manny)

```
install -m 0644 /home/trader/council-tools/operations/council-resolve-due.service ~/.config/systemd/user/
install -m 0644 /home/trader/council-tools/operations/council-resolve-due.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
# Dry run first: lists what --apply would execute, runs nothing, writes nothing.
/usr/bin/python3 ~/.claude/knowledge/council-eval/predictions_report.py --study council-legacy resolve-due
systemctl --user enable --now council-resolve-due.timer
systemctl --user list-timers council-resolve-due.timer
```

## Verify after the first run

```
systemctl --user status council-resolve-due.service
journalctl --user -u council-resolve-due.service -n 50
```

The last line of the output is a summary: `{"applied": true, "due": N, "errors": 0, "summary": true}`.
Exit 1 means a grade could not be written for a reason other than another
session grading the same outcome first (that race prints `graded-elsewhere`
and is not a failure), the ledger is invalid, or the shim's pin does not
match. Any of those fails the unit and `OnFailure=onduty-failed@%n.service`
sends a Pushover; read the journal before concluding the ledger is damaged.

## Remove

```
systemctl --user disable --now council-resolve-due.timer
rm ~/.config/systemd/user/council-resolve-due.{service,timer}
systemctl --user daemon-reload
```

Removing the timer loses nothing. Step 1 of every council still runs
`resolve-due --apply`.

Use this timer, never a second scheduler beside it: two would run the same
checks twice, and the loser of each race records nothing.
