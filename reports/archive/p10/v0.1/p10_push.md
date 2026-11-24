# Report: p10_push

## Run

- Repository: `SAIL-Reply/ipi-twins` (private: True)
- Tag: `v0.1`
- Configs: `bipia`, `nemotron`
- Last action: verify

## Preflight

Step reports P1-P5 have no failed checks; every file has the 29 schema columns in order, the reported row count, classes 1:1, one split and source value, and unique `sample_id`s.

| file | rows | label 1 | label 0 | MB |
|---|---|---|---|---|
| bipia/train | 19,480 | 9,740 | 9,740 | 9.1 |
| bipia/validation | 3,520 | 1,760 | 1,760 | 1.6 |
| bipia/test | 43,100 | 21,550 | 21,550 | 19.0 |
| nemotron/test | 2,288 | 1,144 | 1,144 | 0.4 |

## Dataset card

`README.md`, sha256 `a7d2158755969d0f…`, staged in `data/interim/p10_stage`; a copy is kept in `reports/p10/p10_dataset_card.md`.

## Secret scan

Staged files: **none found** (token patterns and the actual `HF_TOKEN` / `MISTRAL_API_KEY` values).

## Push

- Commit: `4b54a65b7b39ea91484b61d3e26434f967187802`
- Tag: `v0.1`
- URL: https://huggingface.co/datasets/SAIL-Reply/ipi-twins/commit/4b54a65b7b39ea91484b61d3e26434f967187802

## Verify (clean environment, `load_dataset`)

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

- **Early, partial push (v0.1, team decision 2026-11-23):** the brief pushes once, after P7 and P8. This version has `bipia` and `nemotron` so that training can start; `bipia_textfooler` follows in the final push (v1.0).
- **Repository name:** `SAIL-Reply/ipi-twins` instead of `<org>/ipi-shieldstral` (team decision).
- **Step 1:** the P7 and P8 reports do not exist yet, so the preflight reads the P1-P5 reports and stops on any failed check.
- **Verify:** the clean environment is `uv run --isolated --no-project --with datasets` with a fresh Hugging Face cache.
