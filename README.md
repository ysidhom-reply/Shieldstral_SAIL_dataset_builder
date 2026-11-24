# ipi-twins dataset builder

Builds the private Hugging Face dataset
[`SAIL-Reply/ipi-twins`](https://huggingface.co/datasets/SAIL-Reply/ipi-twins). It is used to
train and benchmark Shieldstral as a guard against indirect prompt injection and TextFooler-style
rewording. Every attacked sample has a benign twin: same document and query, harmless text at
the same place.

| Config | Splits | Built by |
|---|---|---|
| `bipia` | train / validation / test | P1-P4 |
| `nemotron` | test | P5 |
| `bipia_textfooler` | test (train / validation later) | P6 (P8) |

Progress, decisions and deviations: [reports/working_report.md](reports/working_report.md).

## Setup

```bash
uv sync                          # Python 3.12 + all deps (incl. dev)
uv run pre-commit install        # optional
uv run dataset-builder check-config
```

All settings live in [configs/config.yaml](configs/config.yaml): seed 42, paths, fixed texts,
models, filters, limits and expected counts. P3's prompts are in
[configs/p3_prompts.yaml](configs/p3_prompts.yaml) (every prompt is versioned, and the version is
recorded on each generated text).

Secrets come from a git-ignored `.env` file at the repository root (copy `.env.example`), or from
an environment variable of the same name, which takes precedence. They are never logged, cached
or written by the code:

| Variable | Needed for |
|---|---|
| `MISTRAL_API_KEY` | P3 (write and judge benign texts) and P6 (judge the variants) |
| `HF_TOKEN` | P10 push and verify only (fine-grained, write access to `SAIL-Reply/ipi-twins`) |

### Environments

- **Main environment** (`uv sync`): every step except P6.
- **P6 environment:** TextAttack needs `transformers<5`, hence `huggingface-hub<1`, while the
  project lock uses `huggingface-hub` 2.x (P5, P10). P6 therefore runs in its own isolated uv
  environment, defined in [requirements/p6-textfooler.txt](requirements/p6-textfooler.txt):

  ```bash
  uv run --isolated --no-project --with-editable . \
    --with-requirements requirements/p6-textfooler.txt dataset-builder p6-textfooler
  ```

## Layout

```
configs/
  config.yaml           single source of truth (seed, paths, texts, filters, limits, counts)
  p3_prompts.yaml       P3 prompts A/B, hard-negative prompts A-hn/B-hn, judges (versioned)
  p5_sentence_repairs.yaml  hand decisions for Nemotron mid-sentence cuts
requirements/
  p6-textfooler.txt     the isolated P6 environment (TextAttack, TensorFlow)
data/raw/               downloads: BIPIA, Nemotron, NLTK data (git-ignored)
data/interim/           intermediate files and API caches (git-ignored)
data/out/               final configs; never overwritten without --force (git-ignored)
reports/
  p1/ ... p10/          the current .md and .json report of each step
  archive/              superseded reports (pilots, one-off migrations, earlier pushes)
  working_report.{md,pdf,html}
src/dataset_builder/
  config.py             load_config(), require_secret()
  schema.py             Sample model, COLUMNS (the shared contract), instruct_text()
  io.py                 prepare_out() (--force guard), jsonl/parquet I/O, rng(step), pilot()
  reports.py            write_counts_report(), check_expected(), report_path()
  insert.py             insert() / remove(): the one insertion rule (P2, P4, P7)
  benign_filters.py     P3 filters (shape, forbidden content, duplicates)
  mistral_client.py     cached, retried Mistral API client
  steps/                one module per step (p1_..., p10_...)
  cli.py                Typer CLI
tests/                  one test file per step
```

## Running a step

Global options go before the command:

```bash
uv run dataset-builder [--pilot | --full] [--force] [--config PATH] <command>
```

- `--pilot` is the default for every step that calls an API or runs long. It runs on
  `pilot_size` (20) documents and writes `*_pilot*` files. Read the report, then re-run with
  `--full`.
- Every step writes a report in `reports/<step>/` and checks its counts against
  `expected_counts` in the config. If a count differs from the brief, the step stops with
  `CountMismatchError`. Counts are never adjusted silently.
- Randomness comes from `io.rng("<step>")`: one deterministic stream per step, derived from the
  seed. Every API call is cached on disk, so a re-run costs nothing and gives the same output.

## Steps

| Step | Command | Outputs |
|---|---|---|
| P1 BIPIA clean and split | `p1-bipia-split` | `data/interim/bipia_docs.jsonl`, `data/interim/attacks.jsonl`, `reports/p1/p1_counts.md` |
| P2 BIPIA attacked samples (label 1) | `p2-bipia-positives [--n-per-doc 20]` | `data/interim/bipia_positives_{train,validation,test}.parquet`, `reports/p2/p2_counts.md` |
| P3 BIPIA benign text pool (Mistral) | `p3-benign-pool` (pilot), then `--full p3-benign-pool` | `data/interim/benign_pool.jsonl`, `reports/p3/p3_rejections.md`, `reports/p3/p3_handread.md` |
| P3 option B: hard negatives | `p3-benign-pool --hard-negatives` (pilot), then `--full p3-benign-pool --hard-negatives` | replaces 40% of every pool in `benign_pool.jsonl` (original kept as `benign_pool_v1.jsonl`), `reports/p3/p3_hard_negatives.md` |
| P4 BIPIA negatives + `bipia` config | `p4-bipia-assemble` (`--force` to overwrite) | `data/out/bipia/{train,validation,test}.parquet` (with `benign_span`), `data/interim/p4_pairs.jsonl`, `reports/p4/p4_composition.md` |
| P5 Nemotron test set | `--full p5-nemotron` | `data/out/nemotron/test.parquet`, `reports/p5/p5_nemotron.md` |
| P6 TextFooler test variants (P6 environment) | `p6-textfooler` (pilot, 20 samples), then `--full p6-textfooler` | `data/out/bipia_textfooler/test.parquet`, `data/interim/p6_results.jsonl` (resumable), `reports/p6/p6_textfooler.md` |
| P7 checks and shortcut baselines | `p7-checks --version v0` (v1, v2 later) | `reports/p7/p7_checks_<version>.{md,json}`; exit 0 = PUSH ALLOWED, 1 = a data check failed, 2 = a baseline limit exceeded |
| P10 push to Hugging Face | `p10-push` (dry-run), then `p10-push --push --tag <version>`, then `p10-verify --tag <version>` | `data/interim/p10_stage/` (card + files), `reports/p10/p10_push.md`, `reports/p10/p10_dataset_card.md` |

Every command is run as `uv run dataset-builder <command>` (P6: in the P6 environment, see above).

P1 clones BIPIA into `data/raw/BIPIA` and checks it is at the commit pinned in
`bipia.repo_commit`. Only data files are read; no BIPIA code is run.

P5 downloads the public Nemotron IPI file (no token) into `data/raw/nemotron` at the revision
pinned in `nemotron.revision`. Mid-sentence cuts must be decided by hand in
`configs/p5_sentence_repairs.yaml`. The default `--pilot` run reports on 20 records and writes
nothing to `data/out`.

## Rules

- No LLM perturbs text. Mistral Small only writes the benign texts (P3); Mistral Medium only
  judges: the benign texts (P3) and the TextFooler variants (P6).
- P3's original texts use the brief's filters. The hard negatives (option B) use a narrower
  profile (`p3.hard_negatives` in the config) and judge-v4; see the P3 reports.
- Test documents get only BIPIA's test attacks. Train and validation documents get only the
  train attacks. No document appears in two splits.
- P7 only reads `data/out` and writes reports: it never changes the data or a limit.
- Nothing is pushed to Hugging Face outside P10, and a push needs the P7 report of that version
  to say **PUSH ALLOWED**. `p10-push` is a dry-run unless `--push` is given; it stops if a step
  report has a failed check, a count differs, a column is not in the schema, or a staged file
  contains something that looks like a secret. It refuses a public repo or an existing tag.
- **v0.1 (team decision, 2026-11-23):** `bipia` and `nemotron` were pushed before P6-P9 so that
  training could start. `data/out` has changed since (`benign_span`, hard negatives), so the next
  push is a new version.

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
```
