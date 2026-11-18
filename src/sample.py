import argparse
import json
import random
from pathlib import Path

from huggingface_hub import snapshot_download

parser = argparse.ArgumentParser(description="Sample DecisionPivot evenly across every task.")
parser.add_argument("--size", type=int, default=480)
parser.add_argument("--seed", type=int, default=20261117)
parser.add_argument("--out", type=Path, default=Path("sample.jsonl"))
args = parser.parse_args()

root = Path(snapshot_download("startlux-models/DecisionPivot", repo_type="dataset", allow_patterns="tasks/*/items.jsonl"))
rng = random.Random(args.seed)
pools = []
for path in sorted(root.glob("tasks/*/items.jsonl")):
    items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rng.shuffle(items)
    pools.append(items)

sample = []
while len(sample) < args.size and any(pools):
    for items in pools:
        if items and len(sample) < args.size:
            sample.append(items.pop())


def gold(item):
    if item["gold"] is not None:
        return str(item["gold"])
    return max(item["soft"], key=item["soft"].get)


with args.out.open("w") as out:
    for item in sample:
        body = {"state": item["state"], "questions": item["questions"]}
        out.write(json.dumps({"id": item["id"], "family": item["id"].split(":")[0], "body": body, "gold": {"q": gold(item)}}) + "\n")

print(f"wrote {len(sample)} items from {len(pools)} tasks to {args.out}")
