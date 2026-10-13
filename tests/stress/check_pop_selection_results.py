# SPDX-FileCopyrightText: 2026 Tazlin <tazlin.on.github@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Gate a pop-selection scenario run on its oracle evidence and coverage.

The scenario writes one JSONL record per oracle violation and a final ``run_summary`` record holding, for each
oracle, how many cases it had to judge. The gate passes only when:

- no violation was recorded, and
- every behaviour the scenario exists to exercise was actually reached: a run whose priority workers never served a
  priority request behind a deep unservable queue, or whose swarm never popped concurrently, proves nothing however
  clean its evidence is. ``--min`` overrides a floor (``--min priority.served_behind_continued_read=0``) and
  ``--skip-coverage`` drops the floors entirely for runs that disable lanes.

With ``--expect`` the gate instead requires at least one violation of each named oracle, which proves the scenario
still elicits a known defect against a server that has it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

_SAMPLE_LIMIT = 5

# The coverage each lane must reach for its oracles to have judged something. Keys are run_summary coverage keys.
DEFAULT_FLOORS: dict[str, int] = {
    "overclaim.handouts_checked": 200,
    "turned_away.empty_pops_checked": 50,
    "status.reads_checked": 200,
    "quiescent.requests_checked": 50,
    "done.requests_read_again_after_done": 50,
    "attribution.requests_checked": 50,
    "priority.priority_served": 20,
    "priority.served_behind_first_read": 5,
    "priority.served_behind_continued_read": 1,
    "order.handouts_with_queue_behind": 10,
    "targeting.targeted_handouts": 3,
    "targeting.excluding_handouts": 3,
    "maintenance.handouts": 1,
    "paused.fake_handouts": 1,
    "paused.handouts_after_unpause": 1,
    "blacklist.handouts": 20,
    "skipped.blacklist_reports_checked": 1,
    "cancel.handouts_of_cancelled": 0,
    "probe.sent": 20,
}


def read_evidence(path: Path) -> tuple[list[dict], dict | None]:
    violations: list[dict] = []
    summary = None
    if not path.is_file():
        return violations, summary
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("record") == "run_summary":
                summary = record
            else:
                violations.append(record)
    return violations, summary


def read_request_count(stats_path: Path) -> int:
    with stats_path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("Name") == "Aggregated":
                return int(row.get("Request Count", "0") or "0")
    return 0


def parse_floor(value: str) -> tuple[str, int]:
    key, _, number = value.partition("=")
    if not key or not number.isdigit():
        raise argparse.ArgumentTypeError(f"expected key=count, got {value!r}")
    return key, int(number)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--stats", type=Path, default=None, help="Locust <prefix>_stats.csv; a run with no requests fails.")
    parser.add_argument("--min", action="append", type=parse_floor, default=[], metavar="KEY=COUNT", help="Override a coverage floor.")
    parser.add_argument("--skip-coverage", action="store_true", help="Do not enforce coverage floors.")
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="ORACLE",
        help="Require at least one violation of this oracle (e.g. oracle:pop_overclaim) instead of none at all.",
    )
    args = parser.parse_args(argv)

    violations, summary = read_evidence(args.evidence)
    failures: list[str] = []
    if summary is None:
        failures.append("the evidence has no run_summary record: the run did not reach its final judgement")
        summary = {"coverage": {}}

    counts = Counter(str(record.get("oracle")) for record in violations)
    print(f"Pop selection evidence: {len(violations)} violation(s).")
    for oracle, number in sorted(counts.items()):
        print(f"  {number:>6}x {oracle}")
    for oracle in sorted(counts):
        print(f"--- up to {_SAMPLE_LIMIT} samples of {oracle} ---", file=sys.stderr)
        for record in [record for record in violations if str(record.get("oracle")) == oracle][:_SAMPLE_LIMIT]:
            print(f"  {json.dumps(record, sort_keys=True)[:1500]}", file=sys.stderr)

    coverage = summary.get("coverage", {})
    print("Coverage:")
    for key in sorted(set(coverage) | set(DEFAULT_FLOORS)):
        print(f"  {coverage.get(key, 0):>8}  {key}")
    for key in ("requests", "pops", "handouts", "untracked_handouts", "final_status_reads", "evaluation_seconds"):
        if key in summary:
            print(f"  {summary[key]:>8}  summary.{key}")

    if args.stats is not None:
        if not args.stats.is_file():
            failures.append(f"stats file {args.stats} not found")
        elif read_request_count(args.stats) <= 0:
            failures.append("the run drove no requests")

    if not args.skip_coverage:
        floors = dict(DEFAULT_FLOORS)
        floors.update(dict(args.min))
        for key, floor in sorted(floors.items()):
            if coverage.get(key, 0) < floor:
                failures.append(f"coverage {key} = {coverage.get(key, 0)}, below its floor of {floor}")

    if args.expect:
        for oracle in args.expect:
            if counts.get(oracle, 0) == 0:
                failures.append(f"expected at least one {oracle} violation and the run recorded none")
    elif violations:
        failures.append(f"{len(violations)} oracle violation(s) recorded")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
