"""Read-only, operator-owned cross-run repair worksheets; never review authority."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

from .findings import SEAT_OWNED_FINDING_KEYS


class ClosureError(ValueError):
    pass


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ClosureError(f"{label}: unexpected fields")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ClosureError(f"{label}: nonempty text required")
    return value


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ClosureError("candidate must be a full commit")
    return value


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ClosureError("duplicate JSON field: " + key)
        result[key] = value
    return result


def _artifact(ref, root):
    _keys(ref, ("path", "bytes", "sha256"), "artifact")
    relative = Path(_text(ref["path"], "artifact path"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ClosureError("artifact must be inside the evidence root")
    path = root / relative
    if path.resolve(strict=True) != path or not path.is_file():
        raise ClosureError("artifact aliases are refused")
    payload = path.read_bytes()
    if type(ref["bytes"]) is not int or len(payload) != ref["bytes"] or (
            hashlib.sha256(payload).hexdigest() != ref["sha256"]):
        raise ClosureError("artifact bytes or digest mismatch")
    return payload


def _pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ClosureError("source pointer must be an absolute JSON pointer")
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            document = document[int(part)] if isinstance(document, list) else document[part]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ClosureError("source pointer does not resolve") from exc
    return document


def validate_worksheet(value, *, evidence_root: Path):
    """Verify provenance, not whether an operator's technical closure is correct."""
    root = evidence_root.absolute()
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ClosureError("evidence root must be a real directory")
    _keys(value, ("schemaVersion", "entries"), "worksheet")
    if value["schemaVersion"] != 1 or not isinstance(value["entries"], list):
        raise ClosureError("unsupported worksheet")
    seen = set()
    counts = Counter()
    linked = []
    for row in value["entries"]:
        _keys(row, ("familyId", "source", "sourcePointers", "runId", "candidateCommit",
                    "finding", "repairedRange", "reproduction", "positiveControl",
                    "adjacentPaths", "disposition", "reason"), "entry")
        _text(row["familyId"], "operator family")
        _text(row["reason"], "operator reason")
        _sha(row["candidateCommit"])
        if not isinstance(row["runId"], str) or not re.fullmatch(r"run-[0-9a-f]{32}", row["runId"]):
            raise ClosureError("runId must be an original run identifier")
        _keys(row["finding"], SEAT_OWNED_FINDING_KEYS, "seat-owned finding")
        source = json.loads(_artifact(row["source"], root), object_pairs_hook=_unique_json)
        _keys(row["sourcePointers"], ("runId", "candidateCommit", "finding"), "source pointers")
        for key, pointer in row["sourcePointers"].items():
            if _pointer(source, pointer) != row[key]:
                raise ClosureError("source-bound field changed: " + key)
        identity = (row["runId"], _text(row["finding"]["findingId"], "findingId"))
        if identity in seen:
            raise ClosureError("duplicate original finding")
        seen.add(identity)
        if row["disposition"] not in ("OPEN", "OPERATOR_CLOSED", "DEFERRED"):
            raise ClosureError("unknown operator disposition")
        if not isinstance(row["adjacentPaths"], list) or not row["adjacentPaths"]:
            raise ClosureError("adjacent path inventory required")
        for path in row["adjacentPaths"]:
            _text(path, "adjacent path")
        repair = row["repairedRange"]
        if repair is not None:
            _keys(repair, ("base", "head"), "repair range")
            _sha(repair["base"])
            _sha(repair["head"])
            if repair["base"] == repair["head"]:
                raise ClosureError("repair range must contain a change")
        for key in ("reproduction", "positiveControl"):
            if row[key] is not None:
                _artifact(row[key], root)
        if row["disposition"] == "OPERATOR_CLOSED" and any(
                row[key] is None for key in ("repairedRange", "reproduction", "positiveControl")):
            raise ClosureError("operator closure requires repair and both controls")
        counts[row["disposition"]] += 1
        linked.append({"runId": row["runId"], "findingId": identity[1],
                       "familyId": row["familyId"], "disposition": row["disposition"]})
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {"status": "PROVENANCE_VALIDATED_OPERATOR_WORKSHEET", "entries": linked,
            "worksheetSha256": hashlib.sha256(canonical).hexdigest(),
            "operatorDispositionCounts": dict(counts), "historicalVerdictsChanged": False,
            "technicalClosureVerified": False, "authorizationEffect": False,
            "causalEffectEstimated": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worksheet", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.worksheet.read_bytes(), object_pairs_hook=_unique_json)
        result = validate_worksheet(value, evidence_root=args.evidence_root)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"closure worksheet refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
