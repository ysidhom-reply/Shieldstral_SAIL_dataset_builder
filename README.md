# dataset-builder

Tooling to build, validate and export datasets.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages Python, the virtual environment and dependencies)

## Setup

```bash
uv sync                      # creates .venv with Python 3.12 and installs all deps (incl. dev)
cp .env.example .env         # then edit .env
uv run pre-commit install    # optional: lint/format on commit
```

## Usage

Put raw `*.jsonl` files (one JSON object per line, with at least `id` and `text`) in `data/raw/`, then:

```bash
uv run dataset-builder build
```

Splits are written to `data/processed/<DATASET_NAME>/{train,validation,test}.jsonl`.
Options override `.env` values: `uv run dataset-builder build --raw-dir ... --out-dir ... --seed 7`.

## Project layout

```
├── data/
│   ├── raw/          # source data (git-ignored)
│   ├── interim/      # intermediate artifacts (git-ignored)
│   └── processed/    # final dataset (git-ignored)
├── src/dataset_builder/
│   ├── cli.py        # Typer CLI entry point
│   ├── config.py     # settings from env / .env (DATASET_* prefix)
│   ├── schema.py     # Pydantic record schema
│   └── pipeline.py   # load -> validate -> transform -> split -> export
└── tests/
```

## Development

```bash
uv add <package>             # add a runtime dependency
uv add --dev <package>       # add a dev dependency
uv run pytest                # tests
uv run ruff check . && uv run ruff format .
```

## License

Licensed under the [Apache License 2.0](LICENSE).
