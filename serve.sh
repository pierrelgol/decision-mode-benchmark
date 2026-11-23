#!/usr/bin/env bash
set -euo pipefail

MODEL_PATH="$HOME/model/StartLux-Decision-v1.1-0.8B-Instant"

[[ -f "$MODEL_PATH/decision_config.json" ]] || { echo "not a StartLux model: $MODEL_PATH" >&2; exit 1; }

# vLLM has no Metal backend, so macOS serves through MLX.
if [[ "$(uname -s)" == "Darwin" ]]; then
    extra=mlx
    engine=(--backend mlx)
else
    extra=vllm
    engine=(--launch-vllm --gpus 0)
fi

exec uvx --python 3.12 --from "startlux-decide[$extra] @ git+https://github.com/StartLuxLabs/StartLux-Decision" \
    startlux-decide serve --model "$MODEL_PATH" "${engine[@]}" --port 8090
