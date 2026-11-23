import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


WARMUP_TIMEOUT_S = 1800


def call(url, method, body, headers, timeout):
    data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def base_url(url):
    return url.rstrip("/").removesuffix("/v1/systemone")


def model_from(payload):
    entries = payload.get("data") or payload.get("models") or []
    names = [entry if isinstance(entry, str) else entry.get("id") or entry.get("name") for entry in entries]
    if len(names) != 1:
        raise SystemExit(f"/v1/models lists {names}: pick one with --model")
    return names[0]


def warmup(base, log_path):
    script = Path(__file__).with_name("spec_tester.py")
    try:
        with log_path.open("w") as log:
            code = subprocess.run([sys.executable, str(script), base], stdout=log, stderr=subprocess.STDOUT, timeout=WARMUP_TIMEOUT_S).returncode
        print(f"warmup: spec tester exited {code}, log in {log_path}")
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"warmup: stopped ({error!r}), benchmarking anyway")


def option_keys(question, kind):
    if kind == "score":
        return [str(i) for i in range(len(question["criteria"]))]
    return list(question["criteria"])


def check_answer(qid, question, answer, body, problems):
    kind = {"bool": "noul"}.get(question["type"], question["type"])
    if answer.get("type") != kind:
        problems.append(f"{qid}: type {answer.get('type')!r}, want {kind!r}")
        return None
    if kind == "noul":
        if not 0 <= answer.get("noul", -1) <= 1:
            problems.append(f"{qid}: noul out of [0, 1]")
            return None
        return "yes" if answer["noul"] > 0.5 else "no"

    keys = option_keys(question, kind)
    probabilities = answer.get("probabilities") or {}
    if sorted(probabilities) != sorted(keys) or abs(sum(probabilities.values()) - 1) > 1e-3:
        problems.append(f"{qid}: probabilities do not cover the options or do not sum to 1")
    if not 0 <= answer.get("confidence", -1) <= 1:
        problems.append(f"{qid}: confidence out of [0, 1]")
    if kind == "score":
        if len(answer.get("legend") or {}) != len(keys) or not 0 <= answer.get("score", -1) <= len(keys) - 1:
            problems.append(f"{qid}: bad legend or score")
        return max(keys, key=lambda key: probabilities.get(key, 0.0))

    allow_none = question.get("allow_none")
    if allow_none is None:
        allow_none = body.get("allow_none")
    if (allow_none or body.get("none_check") is True) and "none_probability" not in answer:
        problems.append(f"{qid}: none_probability missing")
    if answer.get("choice") not in keys and not (allow_none and answer.get("choice") is None):
        problems.append(f"{qid}: choice {answer.get('choice')!r} is not an option")
    return answer.get("choice")


def check(row, status, headers, payload, expected_status):
    problems = []
    if status != expected_status:
        return {}, [f"status {status}, want {expected_status}"]
    if status >= 400:
        return {}, [] if isinstance(payload, dict) and isinstance(payload.get("error"), str) else ["error body without 'error'"]
    problems += [f"missing {key!r}" for key in row.get("keys", []) if key not in payload]
    request_id = row.get("headers", {}).get("x-request-id")
    lowered = {key.lower(): value for key, value in headers.items()}
    if request_id and (lowered.get("x-request-id") != request_id or lowered.get("x-typesafe-request-id") != request_id):
        problems.append("x-request-id not echoed")
    body = row.get("body")
    if not isinstance(body, dict):
        return {}, problems

    answers = payload.get("answers") or {}
    details = body.get("return_details") or body.get("return_thought")
    if not isinstance(payload.get("usage"), dict) or not isinstance(payload.get("model"), str):
        problems.append("missing usage or model")
    if details and "timing_ms" not in payload:
        problems.append("timing_ms missing")
    predicted = {}
    for qid, question in body["questions"].items():
        if qid not in answers:
            problems.append(f"{qid}: no answer")
            continue
        if answers[qid].get("skipped") is True:
            predicted[qid] = "skipped"
            if "ask_if" not in question:
                problems.append(f"{qid}: skipped without ask_if")
            continue
        predicted[qid] = check_answer(qid, question, answers[qid], body, problems)
        detail = answers[qid].get("details")
        if details and detail is None:
            problems.append(f"{qid}: details missing")
        if body.get("return_thought") and detail and (detail.get("thought") or {}).get("used") and "text" not in detail["thought"]:
            problems.append(f"{qid}: thought text missing")
    for qid, want in (row.get("gold") or {}).items():
        if predicted.get(qid, "missing") != want:
            problems.append(f"{qid}: answered {predicted.get(qid)!r}, gold {want!r}")
    return predicted, problems


def expected_status(row, health):
    if row.get("needs") == "thinking" and health.get("kind") == "instant":
        return 422
    if row.get("needs") == "images" and health.get("images") is False:
        return 422
    return row.get("status", 200)


def run(base, row, health, timeout, t0):
    started = time.perf_counter()
    method = row.get("method", "POST")
    try:
        status, headers, raw = call(base + row.get("path", "/v1/systemone"), method, row.get("body"), row.get("headers", {}), timeout)
        latency_ms = (time.perf_counter() - started) * 1000
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw.decode(errors="replace")
        predicted, problems = check(row, status, headers, payload, expected_status(row, health))
    except Exception as error:
        latency_ms, status, payload, predicted, problems = (time.perf_counter() - started) * 1000, 0, None, {}, [repr(error)]
    gold = row.get("gold")
    reply = payload if isinstance(payload, dict) else {}
    return {"id": row["id"], "family": row["family"], "ok": 0 < status < 500, "status": status, "accepted": not problems,
            "correct": bool(gold) and all(predicted.get(qid, "missing") == want for qid, want in gold.items()),
            "problems": problems, "predicted": predicted, "gold": gold, "latency_ms": latency_ms,
            "started_s": started - t0, "finished_s": time.perf_counter() - t0,
            "answers": reply.get("answers"), "usage": reply.get("usage")}


def pct(values, p):
    return statistics.quantiles(values, n=100, method="inclusive")[p - 1] if len(values) > 1 else values[0]


def summary(name, rows):
    correct = sum(r["correct"] for r in rows)
    return f"  {name:<28} {correct:>4}/{len(rows):<4} {100 * correct / len(rows):5.1f}%"


def fetch_health(base):
    try:
        health = json.loads(call(base + "/health", "GET", None, {}, 10)[2])
        health = health if isinstance(health, dict) else {}
    except Exception as error:
        health = {}
        print(f"no /health ({error!r}): thinking and image requests expect 200")
    print(f"server {health.get('model')}  kind {health.get('kind')}  images {health.get('images')}")
    return health


def main():
    parser = argparse.ArgumentParser(description="Send the DecisionPivot sample to a /v1/systemone server.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--model", help="default: the only model in /v1/models")
    parser.add_argument("-c", "--concurrency", type=int, default=8)
    parser.add_argument("--sample", type=Path, default=Path("sample.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("runs"))
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    base = base_url(args.url)
    health = fetch_health(base)
    model = args.model or model_from(json.loads(call(base + "/v1/models", "GET", None, {}, 10)[2]))
    items = [json.loads(line) for line in args.sample.read_text().splitlines() if line.strip()]
    for item in items:
        item["body"].setdefault("model", model)
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"{args.name}.jsonl"
    warmup(base, args.out / f"{args.name}.warmup.log")

    rows = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(args.concurrency) as pool, out_path.open("w") as out:
        for future in as_completed([pool.submit(run, base, row, health, args.timeout, t0) for row in items]):
            row = {"run": args.name, "model": model, "concurrency": args.concurrency, **future.result()}
            rows.append(row)
            out.write(json.dumps(row) + "\n")
            correct = 100 * sum(r["correct"] for r in rows) / len(rows)
            sys.stdout.write(f"\r[{len(rows):>4}/{len(items)}] correct {correct:5.1f}%  last {row['status']} {row['latency_ms']:8.1f} ms ")
            sys.stdout.flush()
    wall = time.perf_counter() - t0
    print()

    ok = [r for r in rows if r["ok"]]
    latencies = sorted(r["latency_ms"] for r in ok) or [0.0]
    by_family = defaultdict(list)
    for r in rows:
        by_family[r["family"]].append(r)

    print(f"\n{args.name}  {base}  model {model}  c={args.concurrency}")
    print(f"  requests   {len(rows)}  answered {len(ok)}  transport/5xx errors {len(rows) - len(ok)}")
    print(f"  throughput {len(ok) / wall:.2f} req/s  wall {wall:.1f} s")
    print(f"  latency    mean {statistics.fmean(latencies):.1f}  p50 {pct(latencies, 50):.1f}  p95 {pct(latencies, 95):.1f}  p99 {pct(latencies, 99):.1f} ms")
    for family, family_rows in sorted(by_family.items()):
        print(summary(family, family_rows))
    print(summary("total correct", rows))
    print(f"  spec + gold accepted {sum(r['accepted'] for r in rows)}/{len(rows)}")
    print(f"  saved {out_path}")


if __name__ == "__main__":
    main()
