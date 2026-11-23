# ipi-shieldstral dataset builder

Builds the private Hugging Face dataset `ipi-shieldstral`. It is used to train and benchmark
Shieldstral as a guard against indirect prompt injection and TextFooler-style rewording.

| Config | Splits |
|---|---|
| `bipia` | train / validation / test |
| `nemotron` | test |
| `bipia_textfooler` | test (train / validation later) |

## Setup

```bash
uv sync                          # Python 3.12 + all deps (incl. dev)
uv run pre-commit install        # optional
uv run dataset-builder check-config
```

All settings live in [configs/config.yaml](configs/config.yaml): seed 42, paths, fixed texts,
models and expected counts.

Secrets come from a git-ignored `.env` file at the repository root (copy `.env.example`), or from an environment variable of the same name, which takes precedence. They are never logged, cached or written by the code:

| Variable | Needed for |
|---|---|
| `MISTRAL_API_KEY` | writing and judging benign insertions |
| `HF_TOKEN` | the final push to Hugging Face (P10) only |

## Layout

```
configs/config.yaml   single source of truth (seed, paths, texts, expected counts)
data/raw/             downloads (git-ignored)
data/interim/         intermediate files, e.g. bipia_docs.jsonl (git-ignored)
data/out/             final configs; never overwritten without --force (git-ignored)
reports/              <step>_counts.{json,md} for every step
src/dataset_builder/
  config.py           load_config(), require_secret()
  schema.py           Sample model, COLUMNS, instruct_text(), build_query()
  io.py               prepare_out() (--force guard), jsonl I/O, rng(step), pilot()
  reports.py          write_counts_report(), check_expected()
  cli.py              Typer CLI
```

## Running a step

Global options go before the command:

```bash
uv run dataset-builder [--pilot | --full] [--force] [--config PATH] <command>
```

- `--pilot` is the default. It runs on `pilot_size` (20) documents. Read the report in
  `reports/`, then re-run with `--full`.
- Every step writes a counts report (rows per scenario, split, class and position) and checks it
  against `expected_counts` in the config. If a count differs from the brief, the step stops
  with `CountMismatchError`. Counts are never adjusted silently.
- Randomness comes from `io.rng("<step>")`: one deterministic stream per step, derived from the
  seed.

## Steps

| Step | Command | Outputs |
|---|---|---|
| P1 BIPIA clean and split | `uv run dataset-builder p1-bipia-split` | `data/interim/bipia_docs.jsonl`, `data/interim/attacks.jsonl`, `reports/p1_counts.md` |
| P2 BIPIA attacked samples (label 1) | `uv run dataset-builder p2-bipia-positives [--n-per-doc 20]` | `data/interim/bipia_positives_{train,validation,test}.parquet`, `reports/p2_counts.md` |
| P3 BIPIA benign text pool (Mistral) | `uv run dataset-builder p3-benign-pool` (pilot, 20 docs), then `uv run dataset-builder --full p3-benign-pool` | `data/interim/benign_pool.jsonl`, `data/interim/benign_rejected.jsonl`, `reports/p3_rejections.md`, `reports/p3_handread.md` (pilot: `*_pilot*`) |
| P4 BIPIA negatives + `bipia` config | `uv run dataset-builder p4-bipia-assemble` (`--force` to overwrite) | `data/out/bipia/{train,validation,test}.parquet`, `data/interim/p4_pairs.jsonl`, `reports/p4_composition.md` |
| P5 Nemotron test set | `uv run dataset-builder --full p5-nemotron` | `data/out/nemotron/test.parquet`, `reports/p5_nemotron.md` |

P1 clones BIPIA into `data/raw/BIPIA` and checks it is at the commit pinned in
`bipia.repo_commit`. Only data files are read; no BIPIA code is run.

P5 downloads the public Nemotron IPI file (no token) into `data/raw/nemotron` at the revision
pinned in `nemotron.revision`. Mid-sentence cuts must be decided by hand in
`configs/p5_sentence_repairs.yaml`. The default `--pilot` run reports on 20 records and writes
nothing to `data/out`.

## Rules

- No LLM is used to perturb text. LLMs only write and judge benign insertions.
- Test documents get only BIPIA's test attacks. Train and validation documents get only the
  train attacks. No document appears in two splits.
- Everything stays in `data/out` until P10. Nothing is pushed to Hugging Face before then.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```
