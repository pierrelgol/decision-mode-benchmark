import argparse
import json
import statistics
from pathlib import Path

parser = argparse.ArgumentParser(description="Compare bench runs.")
parser.add_argument("runs", type=Path, nargs="*")
args = parser.parse_args()

paths = args.runs or sorted(Path("runs").glob("*.jsonl"))
runs = {p.stem: {r["id"]: r for r in map(json.loads, p.read_text().splitlines())} for p in paths}
assert runs, "no runs found"


def pct(values, p):
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1] if len(values) > 1 else values[0]


width = max(map(len, runs)) + 2
print(f"{'run':<{width}}{'n':>5}{'err':>5}{'acc%':>7}{'req/s':>8}{'mean':>9}{'p50':>9}{'p95':>9}{'p99':>9}")
for name, rows in runs.items():
    values = list(rows.values())
    latencies = sorted(r["latency_ms"] for r in values if r["ok"]) or [0.0]
    errors = sum(not r["ok"] for r in values)
    accuracy = 100 * sum(r["accepted"] for r in values) / len(values)
    throughput = (len(values) - errors) / max(r["finished_s"] for r in values)
    print(f"{name:<{width}}{len(values):>5}{errors:>5}{accuracy:>7.1f}{throughput:>8.2f}{statistics.fmean(latencies):>9.1f}"
          f"{pct(latencies, 50):>9.1f}{pct(latencies, 95):>9.1f}{pct(latencies, 99):>9.1f}")

families = sorted({r["family"] for rows in runs.values() for r in rows.values()})
print(f"\n{'family':<18}" + "".join(f"{name:>{width}}" for name in runs))
for family in families:
    cells = []
    for rows in runs.values():
        hits = [r["accepted"] for r in rows.values() if r["family"] == family]
        cells.append(f"{100 * sum(hits) / len(hits):.1f}" if hits else "-")
    print(f"{family:<18}" + "".join(f"{cell:>{width}}" for cell in cells))

names = list(runs)
if len(names) > 1:
    print(f"\nagreement on predictions (shared ids)")
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = runs[a].keys() & runs[b].keys()
            same = sum(runs[a][k].get("predicted") == runs[b][k].get("predicted") for k in shared)
            only_a = sum(runs[a][k]["accepted"] and not runs[b][k]["accepted"] for k in shared)
            only_b = sum(runs[b][k]["accepted"] and not runs[a][k]["accepted"] for k in shared)
            print(f"  {a} vs {b}: {same}/{len(shared)} agree, {only_a} only {a} right, {only_b} only {b} right")
