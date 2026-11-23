import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt



def pct(values, p):
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1] if len(values) > 1 else values[0]


parser = argparse.ArgumentParser(description="Plot bench runs.")
parser.add_argument("runs", type=Path, nargs="*")
parser.add_argument("--out", type=Path, default=Path("runs.png"))
args = parser.parse_args()

paths = [p for p in args.runs or sorted(Path("runs").glob("*.jsonl")) if p.stat().st_size]
assert paths, "no runs found"
names, panels = [], {"correct %": [], "throughput req/s": [], "latency p50 ms": [], "latency p95 ms": []}
for path in paths:
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    latencies = sorted(r["latency_ms"] for r in rows if r["ok"]) or [0.0]
    names.append(path.stem)
    panels["correct %"].append(100 * sum(r["correct"] for r in rows) / len(rows))
    panels["throughput req/s"].append(sum(r["ok"] for r in rows) / max(r["finished_s"] for r in rows))
    panels["latency p50 ms"].append(pct(latencies, 50))
    panels["latency p95 ms"].append(pct(latencies, 95))

figure, axes = plt.subplots(1, len(panels), figsize=(4 * len(panels), 4))
for axis, (title, values) in zip(axes, panels.items()):
    bars = axis.bar(names, values, color=[f"C{i}" for i in range(len(names))])
    axis.bar_label(bars, fmt="%.1f")
    axis.set_title(title)
    axis.tick_params(axis="x", rotation=30)
figure.tight_layout()
figure.savefig(args.out, dpi=120)
print(f"wrote {args.out}")
