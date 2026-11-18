# decision-mode-benchmark

Accuracy, latency and spec conformance of a `/v1/systemone` server (StartLux-Decision `docs/api.md`).

The sample is every DecisionPivot task (core and reference tracks, choice and score questions) sampled evenly, each request
sent exactly as published, plus `src/features.jsonl`: one hand-written request per feature of the spec (question types and
criteria forms, JSON states, images, wide choices, `allow_none`, `none_check`, every control, details and thoughts,
`/v1/decisions`, `/v1/models`, `/health`, `x-request-id`, and the 404/405/422 errors).

```sh
uv sync
just sample 480             # src/sample.py  -> sample.jsonl
just bench my-run 8         # src/bench.py   -> runs/my-run.jsonl (name, concurrency)
just compare                # src/compare.py -> table of every run in runs/
just plot                   # src/plot.py    -> runs.png
```

Set the server with `just url=http://host:port bench ...`.

A response is accepted when it has the expected status, every answer has the shape the spec gives for its type, and the
answer matches the gold one: the `choice` for a choice question (null when the gold is "none of the above"), the most
likely level for a score question, yes when `noul` is above 0.5. DecisionPivot items with a soft label take its most
likely option as gold. Requests that think expect a 422 when `/health` reports an Instant model, and requests with
images expect a 422 when it reports a server that reads text only.
