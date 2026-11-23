# decision-mode-benchmark

Accuracy, latency and spec conformance of a `/v1/systemone` server (StartLux-Decision `docs/api.md`).

`sample.jsonl` is every DecisionPivot task (core and reference tracks, choice and score questions) sampled evenly, each request
sent exactly as published. `src/features.jsonl` is the conformance suite, run one by one by `src/test.py`: one hand-written request per feature of the spec (question types and
criteria forms, JSON states, images, wide choices, `allow_none`, `none_check`, every control, details and thoughts,
`/v1/decisions`, `/v1/models`, `/health`, `x-request-id`, and the 404/405/422 errors), plus `ask_if` / `depends_on`
as llmd implements them (a question whose `ask_if` does not hold answers `{"skipped": true}`; cycles, unknown ids and
`ask_if` labels that are not options are 422).

```sh
uv sync
just sample                 # src/sample.py  -> sample.jsonl (1024 requests)
just test                   # src/spec_tester.py -> OK / KO per spec feature
just bench my-run 8         # src/bench.py   -> spec tester warmup, then runs/my-run.jsonl
just compare                # src/compare.py -> table of every run in runs/
just plot                   # src/plot.py    -> runs.png
```

Set the server with `just url=http://host:port bench ...`. Only `uv` and `just` are needed on the machine.

To compare llmd and vLLM, serve the same model with each, then run the same sample against both:

```sh
just url=http://localhost:8000 bench llmd 8
just url=http://localhost:8001 bench vllm 8   # vllm serve MODEL --logprobs-mode raw_logprobs
just compare && just plot
```

Each line of `runs/<name>.jsonl` is one request: `run`, `model`, `concurrency`, `id`, `family`, `status`, `ok`, `correct`,
`accepted`, `problems`, `predicted`, `gold`, `latency_ms`, `started_s`, `finished_s`, `answers` (with probabilities) and `usage`.
Warmup requests are not in it, their output is in `runs/<name>.warmup.log`.

A response is accepted when it has the expected status, every answer has the shape the spec gives for its type, and the
answer matches the gold one: the `choice` for a choice question (null when the gold is "none of the above"), the most
likely level for a score question, yes when `noul` is above 0.5. DecisionPivot items with a soft label take its most
likely option as gold. Requests that think expect a 422 when `/health` reports an Instant model, and requests with
images expect a 422 when it reports a server that reads text only.
