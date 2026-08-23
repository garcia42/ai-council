"""Compatibility reporter for explicitly selected legacy non-council forecast stores."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Iterable


class LegacyReportError(ValueError):
    pass


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise LegacyReportError(
                    f"{path.name} line {line_number}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise LegacyReportError(
                    f"{path.name} line {line_number}: record must be an object"
                )
            rows.append(row)
    return rows


def _key(ts: str, index: int) -> str:
    return f"{ts}#{index}"


def _probability(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise LegacyReportError("legacy probability must be an integer from 0 through 100")
    return value


def resolve(
    log_path: Path,
    resolved_path: Path,
    *,
    ts: str,
    index: int,
    came_true: bool,
    note: str,
) -> None:
    matches = [row for row in _load(log_path) if row.get("ts") == ts]
    if len(matches) != 1:
        raise LegacyReportError(f"expected exactly one legacy row with ts={ts}")
    predictions = matches[0].get("predictions") or []
    if not isinstance(predictions, list) or not 0 <= index < len(predictions):
        raise LegacyReportError(
            f"row {ts} has {len(predictions) if isinstance(predictions, list) else 0} "
            f"predictions; index {index} out of range"
        )
    prediction = predictions[index]
    if not isinstance(prediction, dict):
        raise LegacyReportError("legacy prediction must be an object")
    event = {
        "key": _key(ts, index),
        "ts": ts,
        "index": index,
        "seat": prediction.get("seat"),
        "claim": prediction.get("claim"),
        "probability": _probability(prediction.get("probability")),
        "came_true": came_true,
        "note": note,
    }
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    print(f"recorded: {str(event['claim'])[:70]!r} -> {'TRUE' if came_true else 'FALSE'}")


def report(log_path: Path, resolved_path: Path, *, show_all: bool) -> None:
    rows = _load(log_path)
    resolutions = _load(resolved_path)
    done = {row.get("key") for row in resolutions if row.get("key")}
    today = date.today()
    due = []
    pending = []
    for row in rows:
        ts = row.get("ts", "?")
        predictions = row.get("predictions") or []
        if not isinstance(predictions, list):
            raise LegacyReportError("legacy predictions must be a list")
        for index, prediction in enumerate(predictions):
            if not isinstance(prediction, dict):
                raise LegacyReportError("legacy prediction must be an object")
            if _key(ts, index) in done:
                continue
            raw_date = prediction.get("resolutionDate")
            try:
                resolution_date = date.fromisoformat(raw_date)
            except (TypeError, ValueError):
                due.append((ts, index, prediction, "NO/BAD DATE"))
                continue
            target = due if resolution_date <= today else pending
            target.append((ts, index, prediction, str(resolution_date)))

    if not rows:
        print("legacy forecast log is empty.")
        return
    total = sum(len(row.get("predictions") or []) for row in rows)
    if total == 0:
        print(f"{len(rows)} legacy rows logged, but ZERO predictions recorded.")
        return

    print(f"== DUE FOR SCORING ({len(due)}) ==")
    for ts, index, prediction, when in due:
        print(
            f"  [{ts} #{index}] p={prediction.get('probability')}% due {when}: "
            f"{prediction.get('claim')}"
        )
    if show_all:
        print(f"\n== NOT YET DUE ({len(pending)}) ==")
        for ts, index, prediction, when in pending:
            print(f"  [{ts} #{index}] due {when}: {str(prediction.get('claim'))[:80]}")

    if resolutions:
        scored = []
        for event in resolutions:
            probability = _probability(event.get("probability"))
            came_true = event.get("came_true")
            if not isinstance(came_true, bool):
                raise LegacyReportError("legacy resolution came_true must be Boolean")
            scored.append(((probability / 100) - (1 if came_true else 0)) ** 2)
        hits = sum(1 for event in resolutions if event["came_true"])
        mean_probability = sum(_probability(event["probability"]) for event in resolutions) / len(
            resolutions
        )
        print(f"\n== CALIBRATION ({len(resolutions)} resolved) ==")
        print(f"  hit rate:        {hits}/{len(resolutions)} = {hits / len(resolutions):.0%}")
        print(f"  mean confidence: {mean_probability:.0f}%")
        print(f"  Brier score:     {sum(scored) / len(scored):.3f}")
    else:
        print("\n== CALIBRATION ==\n  no predictions resolved yet.")


def main(
    argv: Iterable[str],
    *,
    log_path: str | Path,
    resolved_path: str | Path,
) -> int:
    args = list(argv)
    log_path = Path(log_path)
    resolved_path = Path(resolved_path)
    try:
        if args and args[0] == "--resolve":
            if len(args) < 4:
                raise LegacyReportError(
                    "usage: --resolve <ts> <index> <true|false> [note]"
                )
            resolve(
                log_path,
                resolved_path,
                ts=args[1],
                index=int(args[2]),
                came_true=args[3].lower() in ("true", "t", "yes", "y", "1"),
                note=args[4] if len(args) > 4 else "",
            )
        else:
            report(log_path, resolved_path, show_all="--all" in args)
        return 0
    except (LegacyReportError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
