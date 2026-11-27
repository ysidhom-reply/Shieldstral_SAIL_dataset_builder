# Report: p10_push_v1

## Run

- Repository: `SAIL-Reply/ipi-twins` (private: True)
- Version / tag: `v1`
- Configs in this version: `bipia`, `nemotron`
- Uploaded by this push: `bipia`, `nemotron` and the card
- Last action: verify

## P7 gate

- P7 v1: **PUSH ALLOWED**. A push is allowed.

## Preflight

Step reports have no failed checks; every local file has the 29 schema columns in order, the reported row count, classes 1:1, one split and source value, and unique `sample_id`s. The staged files keep the 13 Hub columns.

| file | rows | label 1 | label 0 | MB |
|---|---|---|---|---|
| bipia/train | 19,480 | 9,740 | 9,740 | 9.1 |
| bipia/validation | 3,520 | 1,760 | 1,760 | 1.6 |
| bipia/test | 43,100 | 21,550 | 21,550 | 19.0 |
| nemotron/test | 2,288 | 1,144 | 1,144 | 0.4 |

## Dataset card

`README.md`, sha256 `c13dc99a0d771b13…`, staged in `data/interim/p10_stage`; a copy is kept in `reports/p10/p10_dataset_card_v1.md`.

## Secret scan

Staged files: **none found** (token patterns and the actual `HF_TOKEN` / `MISTRAL_API_KEY` values).

## Push

- Commit: `afb093b9fbe10787453bf75b1604f8c86e3ae385`
- Tag: `v1`
- URL: https://huggingface.co/datasets/SAIL-Reply/ipi-twins/commit/afb093b9fbe10787453bf75b1604f8c86e3ae385

## Verify (clean environment, `load_dataset` at revision `v1`)

| config/split | rows loaded |
|---|---|
| bipia/train | 19,480 |
| bipia/validation | 3,520 |
| bipia/test | 43,100 |
| nemotron/test | 2,288 |

- Repository private: True
- Remote files: `.gitattributes`, `README.md`, `bipia/test.parquet`, `bipia/train.parquet`, `bipia/validation.parquet`, `nemotron/test.parquet`
- Secret scan of the remote files: none found
- v0 loaded again at revision `v0`: bipia/train 19,480, bipia/validation 3,520, bipia/test 43,100, nemotron/test 2,288
- v0 files not re-uploaded, compared between `v0` and `v1` (sha256): 0 files, **unchanged**
- Result: **PASSED**

## Deviations from the brief

- **Repository name:** `SAIL-Reply/ipi-twins` instead of `<org>/ipi-shieldstral` (team decision).
- **Verify:** the clean environment is `uv run --isolated --no-project --with datasets` with a fresh Hugging Face cache.
- **Earlier push:** an early `v0.1` (29 columns, original pool) was pushed before the version scheme existed; its record is in `reports/archive/p10/v0.1/`. `v0` replaces it in `main`; the `v0.1` tag keeps it reachable.
- **v1:** the v0 data re-uploaded with `query` = the user's request only (the template wrapper removed by a regex, documents unchanged), no `instruct` column, and only the Hub columns. TextFooler moved to v2 and P8 to v3. The `v0` tag is unchanged.
