import argparse
import json
import sys
import time
from pathlib import Path

from bench import fetch_health, run

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"

parser = argparse.ArgumentParser(description="Send every spec feature request one by one and print OK or KO.")
parser.add_argument("--url", default="http://localhost:8000")
parser.add_argument("--features", type=Path, default=Path(__file__).with_name("features.jsonl"))
parser.add_argument("--timeout", type=float, default=300.0)
args = parser.parse_args()

base = args.url.rstrip("/")
health = fetch_health(base)
rows = [json.loads(line) for line in args.features.read_text().splitlines() if line.strip()]

passed = 0
for index, row in enumerate(rows, 1):
    result = run(base, row, health, args.timeout, time.perf_counter())
    name = row["id"].removeprefix("feature:")
    if result["accepted"]:
        passed += 1
        print(f"{index:>3}. {name:<32} {GREEN}[OK]{RESET} {result['latency_ms']:8.1f} ms")
    else:
        print(f"{index:>3}. {name:<32} {RED}[KO]{RESET} {'; '.join(result['problems'])}")

color = GREEN if passed == len(rows) else RED
print(f"\n{color}{passed}/{len(rows)} passed{RESET}")
sys.exit(passed != len(rows))
