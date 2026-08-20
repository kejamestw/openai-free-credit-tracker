from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from quota_monitor.quality_harness import run_performance_harness, run_simulated_soak


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate repeatable quality evidence.")
    parser.add_argument("mode", choices=("performance", "simulated-soak", "all"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--projects", type=int, default=100)
    parser.add_argument("--hours", type=int, default=72)
    args = parser.parse_args()

    build_root = ROOT / "build" / "quality-harness"
    build_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=build_root) as temporary:
        workspace = Path(temporary)
        reports = []
        if args.mode in {"performance", "all"}:
            reports.append(
                run_performance_harness(
                    workspace / "performance",
                    days=args.days,
                    projects=args.projects,
                )
            )
        if args.mode in {"simulated-soak", "all"}:
            reports.append(run_simulated_soak(hours=args.hours))

    payload = {
        "schema_version": 1,
        "passed": all(report["passed"] for report in reports),
        "reports": reports,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Quality evidence generated: {output}")
    if not payload["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
