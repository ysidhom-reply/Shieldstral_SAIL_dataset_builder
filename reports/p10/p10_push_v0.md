# Report: p10_push_v0

## Run

- Repository: `SAIL-Reply/ipi-twins` (private: True)
- Version / tag: `v0`
- Configs in this version: `bipia`, `nemotron`
- Uploaded by this push:  and the card
- Last action: card update

## P7 gate

- P7 v0: **PUSH ALLOWED**. A push is allowed.

## Preflight

Step reports have no failed checks; every file has the 30 schema columns in order, the reported row count, classes 1:1, one split and source value, and unique `sample_id`s.

| file | rows | label 1 | label 0 | MB |
|---|---|---|---|---|
| bipia/train | 19,480 | 9,740 | 9,740 | 9.1 |
| bipia/validation | 3,520 | 1,760 | 1,760 | 1.7 |
| bipia/test | 43,100 | 21,550 | 21,550 | 19.0 |
| nemotron/test | 2,288 | 1,144 | 1,144 | 0.4 |

## Dataset card

`README.md`, sha256 `8a4b0a60915c0d4d…`, staged in `data/interim/p10_stage`; a copy is kept in `reports/p10/p10_dataset_card_v0.md`.

## Secret scan

Staged files: **none found** (token patterns and the actual `HF_TOKEN` / `MISTRAL_API_KEY` values).

## Push

- Commit: `699b21620ecad6bd33a4333cb05c92018f8f1462`
- Tag: `v0`
- URL: https://huggingface.co/datasets/SAIL-Reply/ipi-twins/commit/699b21620ecad6bd33a4333cb05c92018f8f1462

## Card update

Only `README.md` was uploaded, as a new commit on `main`. No data file was sent and the tag `v0` was not moved.

- Commit: `ef056001762195821ba433ed302dc961d6319fae`
- URL: https://huggingface.co/datasets/SAIL-Reply/ipi-twins/commit/ef056001762195821ba433ed302dc961d6319fae

## Verify (clean environment, `load_dataset` at revision `v0`)

| config/split | rows loaded |
|---|---|
| bipia/train | 19,480 |
| bipia/validation | 3,520 |
| bipia/test | 43,100 |
| nemotron/test | 2,288 |

- Repository private: True
- Remote files: `.gitattributes`, `README.md`, `bipia/test.parquet`, `bipia/train.parquet`, `bipia/validation.parquet`, `nemotron/test.parquet`
- Secret scan of the remote files: none found
- Result: **PASSED**

## Deviations from the brief

- **Repository name:** `SAIL-Reply/ipi-twins` instead of `<org>/ipi-shieldstral` (team decision).
- **Verify:** the clean environment is `uv run --isolated --no-project --with datasets` with a fresh Hugging Face cache.
- **Earlier push:** an early `v0.1` (29 columns, original pool) was pushed before the version scheme existed; its record is in `reports/archive/p10/v0.1/`. `v0` replaces it in `main`; the `v0.1` tag keeps it reachable.
