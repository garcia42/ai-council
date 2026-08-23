#!/usr/bin/env python3
"""Compatibility entry point for the version-controlled council forecast tools."""

import os
import sys

sys.path.insert(0, "/home/trader/council-tools/src")

from council_tools.cli import main  # noqa: E402


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("report")
    elif sys.argv[1] == "--all":
        sys.argv[1] = "report"
    elif sys.argv[1] == "--resolve":
        raise SystemExit(
            "timestamp/index resolution is retired; use resolve <outcomeId> "
            "true|false|void with evidence"
        )
    if sys.argv[1] == "report":
        if "--log" not in sys.argv and os.environ.get("PANEL_LOG"):
            sys.argv.extend(("--log", os.environ["PANEL_LOG"]))
        if "--events" not in sys.argv and os.environ.get("PANEL_RESOLVED"):
            sys.argv.extend(("--events", os.environ["PANEL_RESOLVED"]))
    raise SystemExit(main())
