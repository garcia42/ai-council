"""Evidence-linked status differences without synthesizing activation authority."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .closure import ClosureError, _artifact, _keys, _pointer, _text, _unique_json


def _statuses(document, pointer=""):
    result = {}
    if isinstance(document, dict):
        for key, value in document.items():
            child = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            if isinstance(value, str) and (key.lower().endswith("status") or key == "open_gate"):
                result[child] = value
            result.update(_statuses(value, child))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            result.update(_statuses(value, pointer + "/" + str(index)))
    return result


def build_view(spec, *, evidence_root: Path):
    _keys(spec, ("schemaVersion", "contract", "observations"), "status view")
    if spec["schemaVersion"] != 1 or not isinstance(spec["observations"], list):
        raise ClosureError("unsupported status view")
    root = evidence_root.absolute()
    if root.resolve(strict=True) != root:
        raise ClosureError("evidence root aliases refused")
    contract = json.loads(_artifact(spec["contract"], root), object_pairs_hook=_unique_json)
    declarations = _statuses(contract)
    observations = {}
    for row in spec["observations"]:
        _keys(row, ("contractPointer", "artifact", "evidencePointer", "disposition", "reason"), "observation")
        pointer = row["contractPointer"]
        if pointer not in declarations or pointer in observations:
            raise ClosureError("unknown or duplicate contract status pointer")
        document = json.loads(_artifact(row["artifact"], root), object_pairs_hook=_unique_json)
        observed = _text(_pointer(document, row["evidencePointer"]), "observed status")
        if row["disposition"] not in ("RECONCILE", "EVIDENCE_NEWER", "NOT_COMPARABLE"):
            raise ClosureError("explicit operator disposition required")
        _text(row["reason"], "operator reason")
        observations[pointer] = {**row, "observed": observed}
    axes = []
    for pointer, declared in sorted(declarations.items()):
        row = observations.get(pointer)
        axes.append({"pointer": pointer, "declared": declared,
                     "observed": row["observed"] if row else None,
                     "comparison": "UNOBSERVED" if row is None else
                                   ("MATCH" if row["observed"] == declared else "DIFFERENCE"),
                     "operatorObservation": row})
    return {"schemaVersion": 1, "status": "STATUS_VIEW_NOT_AUTHORITY", "axes": axes,
            "contract": spec["contract"],
            "specSha256": hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "unobservedAxes": sum(a["comparison"] == "UNOBSERVED" for a in axes),
            "differences": sum(a["comparison"] == "DIFFERENCE" for a in axes),
            "unresolvedDifferences": sum(a["comparison"] == "DIFFERENCE" and
                                         a["operatorObservation"]["disposition"] == "RECONCILE" for a in axes),
            "evidenceFreshnessVerified": False, "authorizationEffect": False,
            "runtimeReadinessInferred": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_view(json.loads(args.spec.read_bytes(), object_pairs_hook=_unique_json),
                            evidence_root=args.evidence_root)
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"status view refused: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
