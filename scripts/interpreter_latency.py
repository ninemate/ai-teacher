#!/usr/bin/env python3
"""Summarise Tolmács (live interpreter) latency from its JSON-lines log: p50/p95 per mode and stage.

    python3 scripts/interpreter_latency.py /data/metadata/interpreter_latency.jsonl [--user NAME] [--json]

The log holds figures only (no text, no audio); it is plain JSON lines, so this runs on a copy of it too.
See docs/interpreter.md.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "common"))

from teacher_common.interpreter import STAGES, LatencyLog, summarise  # noqa: E402


def format_table(summary: dict) -> str:
    if not summary:
        return "No measurements in the log."
    lines = []
    for mode, info in summary.items():
        lines.append(f"{mode}: {info['count']} utterances")
        lines.append(f"  {'stage':<14}{'n':>5}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}")
        for stage in STAGES:
            if stage in info["stages"]:
                v = info["stages"][stage]
                lines.append(f"  {stage:<14}{v['n']:>5}{v['p50']:>10}{v['p95']:>10}{v['max']:>10}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", help="path of the interpreter latency log (JSON lines)")
    parser.add_argument("--user", help="only this account's utterances")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    if not Path(args.log).exists():
        print(f"No such log: {args.log}", file=sys.stderr)
        return 2
    summary = summarise(LatencyLog(args.log).read(args.user))
    print(json.dumps(summary, indent=2) if args.json else format_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
