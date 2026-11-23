url := "http://localhost:8000"

default:
    @just --list

sample size="1024" seed="20261117":
    uv run src/sample.py --size {{size}} --seed {{seed}}

bench name concurrency="8":
    uv run src/bench.py --name {{name}} --url {{url}} -c {{concurrency}}

compare *runs:
    uv run src/compare.py {{runs}}

plot *runs:
    uv run src/plot.py {{runs}}

test:
    uv run src/spec_tester.py {{url}}
