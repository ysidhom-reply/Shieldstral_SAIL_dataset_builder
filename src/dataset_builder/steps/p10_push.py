"""P10: push a version of the dataset to the private Hugging Face repo.

Versions (the brief's scheme): v0 = `bipia` + `nemotron`; v1 = v0 + `bipia_textfooler` (test).
A version is pushed once and tagged with its own name. v1 only ADDS `bipia_textfooler` and
updates the card: no v0 file is uploaded again, and `verify` checks that v0 is unchanged.

Dry-run by default: preflight checks, staging (parquet files + dataset card), secret scan, the P7
gate and a report, with no network call. `push` needs the P7 report of the version to say PUSH
ALLOWED; it uploads the staged folder and tags it. `verify` loads every config and split at the
tag from the Hub in a clean environment and compares the counts with the reports.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import yaml

from dataset_builder.config import REPO_ROOT, Config, load_config, require_secret
from dataset_builder.reports import CountMismatchError, report_path
from dataset_builder.schema import COLUMNS, columns_for

REPORT_NAME = "p10_push"
# Splits pushed per config. v1 has only the TextFooler test split (v2 will add train/validation).
SPLITS: dict[str, tuple[str, ...]] = {
    "bipia": ("train", "validation", "test"),
    "nemotron": ("test",),
    "bipia_textfooler": ("test",),
}
VERSIONS: dict[str, tuple[str, ...]] = {
    "v0": ("bipia", "nemotron"),
    "v1": ("bipia", "nemotron", "bipia_textfooler"),
}
# Reports whose `failures` must be empty before anything is pushed (+ P6 for v1).
STEP_REPORTS = (
    "p1/p1_counts.json",
    "p2/p2_counts.json",
    "p3/p3_rejections.json",
    "p4/p4_composition.json",
    "p5/p5_nemotron.json",
)
# Optional reports the card uses when they exist.
OPTIONAL_REPORTS = {"p3hn": "p3/p3_hard_negatives.json"}
SECRET_PATTERNS = {
    "hf_token": re.compile(rb"hf_[A-Za-z0-9]{30,}"),
    "env_assignment": re.compile(rb"(?:HF_TOKEN|MISTRAL_API_KEY)\s*=\s*\S{8,}"),
    "sk_key": re.compile(rb"sk-[A-Za-z0-9]{16,}"),
}


class SecretFoundError(RuntimeError):
    """Raised when a staged or remote file contains something that looks like a secret."""


# ---------------------------------------------------------------- inputs


def stage_dir(cfg: Config) -> Path:
    return cfg.paths.interim / "p10_stage"


def parquet_path(cfg: Config, config: str, split: str) -> Path:
    return cfg.paths.out / config / f"{split}.parquet"


def configs_of(version: str) -> list[str]:
    if version not in VERSIONS:
        raise ValueError(f"unknown version {version!r}: choose from {sorted(VERSIONS)}")
    return list(VERSIONS[version])


def new_configs(version: str) -> list[str]:
    """Configs this version adds to the repo (the others are already there, untouched)."""
    if version == "v0":
        return configs_of("v0")
    return [c for c in configs_of(version) if c not in VERSIONS["v0"]]


def load_reports(cfg: Config, version: str = "v0") -> dict[str, dict[str, Any]]:
    """The step reports' JSON, keyed by step (p1 ... p5, p6 for v1, p3hn and p7 when present)."""
    out: dict[str, dict[str, Any]] = {}
    needed = list(STEP_REPORTS) + (["p6/p6_textfooler.json"] if version == "v1" else [])
    for rel in needed:
        path = cfg.paths.reports / rel
        if not path.is_file():
            raise FileNotFoundError(f"{path} is missing: run that step before P10 {version}.")
        out[rel.split("/")[0]] = json.loads(path.read_text(encoding="utf-8"))
    for key, rel in OPTIONAL_REPORTS.items():
        path = cfg.paths.reports / rel
        if path.is_file():
            out[key] = json.loads(path.read_text(encoding="utf-8"))
    p7 = cfg.paths.reports / "p7" / f"p7_checks_{version}.json"
    if p7.is_file():
        out["p7"] = json.loads(p7.read_text(encoding="utf-8"))
    return out


def expected_rows(reports: dict[str, dict[str, Any]], config: str) -> dict[str, int]:
    """Rows per split that the step reports announce for a config."""
    if config == "bipia":
        return {s: int(n) for s, n in reports["p4"]["split"].items()}
    if config == "nemotron":
        return {s: int(n) for s, n in reports["p5"]["split"].items()}
    if config == "bipia_textfooler":
        return {s: int(n) for s, n in reports["p6"]["split"].items()}
    raise ValueError(f"no report gives the counts of config {config!r} yet")


# ---------------------------------------------------------------- P7 gate


def p7_gate(cfg: Config, version: str, configs: list[str]) -> dict[str, Any]:
    """A push needs the P7 report of its version to say PUSH ALLOWED, and to be newer than the
    data files it checked (so it cannot describe an older build)."""
    path = cfg.paths.reports / "p7" / f"p7_checks_{version}.json"
    if not path.is_file():
        return {
            "ok": False,
            "status": "missing",
            "reason": f"{path.name} not found: run `p7-checks --version {version}`",
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    files = [parquet_path(cfg, c, s) for c in configs for s in SPLITS[c]]
    newest = max((f.stat().st_mtime for f in files if f.is_file()), default=0.0)
    status = data.get("status", "unknown")
    if status != "PUSH ALLOWED" or data.get("exit_code") != 0:
        return {"ok": False, "status": status, "reason": f"P7 {version} says {status}"}
    if path.stat().st_mtime < newest:
        return {
            "ok": False,
            "status": status,
            "reason": f"P7 {version} is older than the data in data/out: run it again",
        }
    return {"ok": True, "status": status, "reason": ""}


class PushBlockedError(RuntimeError):
    """The P7 gate is closed."""


# ---------------------------------------------------------------- preflight


def preflight(
    cfg: Config, configs: list[str], reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Stop unless the step reports passed and the files match them. Returns per-file stats."""
    failed = {step: r["failures"] for step, r in reports.items() if r.get("failures")}
    if failed:
        raise RuntimeError(f"P10 stopped: step reports list failed checks: {failed}")
    unknown = [c for c in configs if c not in SPLITS]
    if unknown:
        raise ValueError(f"unknown configs: {unknown}")

    stats: dict[str, Any] = {}
    errors: list[str] = []
    for config in configs:
        exp = expected_rows(reports, config)
        if set(exp) != set(SPLITS[config]):
            errors.append(f"{config}: reports give splits {sorted(exp)}, expected {SPLITS[config]}")
        for split in SPLITS[config]:
            path = parquet_path(cfg, config, split)
            if not path.is_file():
                errors.append(f"{config}/{split}: {path} is missing")
                continue
            table = pq.read_table(path)
            cols = tuple(table.column_names)
            labels = Counter(table.column("label").to_pylist())
            ids = table.column("sample_id").to_pylist()
            split_col = set(table.column("split").to_pylist())
            source_col = set(table.column("source").to_pylist())
            stats[f"{config}/{split}"] = {
                "rows": table.num_rows,
                "label": {str(k): v for k, v in sorted(labels.items())},
                "bytes": path.stat().st_size,
            }
            if cols != columns_for(config):
                extra = sorted(set(cols) - set(columns_for(config)))
                missing = sorted(set(columns_for(config)) - set(cols))
                errors.append(
                    f"{config}/{split}: columns differ from the schema "
                    f"(extra {extra}, missing {missing}, or a different order)"
                )
            if table.num_rows != exp.get(split):
                errors.append(
                    f"{config}/{split}: {table.num_rows} rows, the report says {exp.get(split)}"
                )
            if labels.get(0, 0) != labels.get(1, 0):
                errors.append(f"{config}/{split}: classes are not 1:1 ({dict(labels)})")
            if len(set(ids)) != len(ids):
                errors.append(f"{config}/{split}: duplicate sample_id")
            if split_col != {split}:
                errors.append(f"{config}/{split}: column split has {sorted(split_col)}")
            if source_col != {config}:
                errors.append(f"{config}/{split}: column source has {sorted(source_col)}")
    if errors:
        raise CountMismatchError("P10 preflight failed:\n  " + "\n  ".join(errors))
    return stats


# ---------------------------------------------------------------- dataset card


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _pct(a: float, b: float) -> str:
    return f"{a / b:.1%}" if b else "n/a"


def _front_matter(cfg: Config, configs: list[str]) -> str:
    meta: dict[str, Any] = {
        "pretty_name": cfg.dataset.pretty_name,
        "license": cfg.dataset.license,
        "language": ["en"],
        "task_categories": ["text-classification"],
        "tags": ["prompt-injection", "indirect-prompt-injection", "llm-security", "guardrails"],
        "size_categories": ["10K<n<100K"],
        "configs": [
            {
                "config_name": c,
                "data_files": [{"split": s, "path": f"{c}/{s}.parquet"} for s in SPLITS[c]],
                **({"default": True} if c == "bipia" else {}),
            }
            for c in configs
        ],
    }
    return "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n"


LICENCE_NAMES = {"cc-by-sa-4.0": "CC BY-SA 4.0", "cc-by-4.0": "CC BY 4.0", "mit": "MIT"}


def hn_share(cfg: Config) -> dict[str, dict[str, float]]:
    """Share of BIPIA negatives whose benign text is a hard negative, per split and scenario
    (read-only: the negatives' benign_id against the style of the pool texts)."""
    pool = cfg.paths.interim / "benign_pool.jsonl"
    if not pool.is_file():
        return {}
    style = {}
    with pool.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                b = json.loads(line)
                style[b["benign_id"]] = b.get("style", "standard")
    if "hard_negative" not in style.values():
        return {}
    out: dict[str, dict[str, float]] = {}
    for split in SPLITS["bipia"]:
        t = pq.read_table(
            parquet_path(cfg, "bipia", split), columns=["label", "scenario", "benign_id"]
        ).to_pylist()
        c: Counter = Counter()
        n: Counter = Counter()
        for r in t:
            if r["label"] == 0:
                n[r["scenario"]] += 1
                c[r["scenario"]] += style.get(r["benign_id"]) == "hard_negative"
        out[split] = {sc: c[sc] / n[sc] for sc in n}
    return out


def _textfooler_sources(reports: dict[str, dict[str, Any]]) -> list[list[str]]:
    info = (reports.get("p6") or {}).get("info", {})
    versions = info.get("versions", {})
    name, _, rev = info.get("victim", "").partition("@")
    return [
        [
            f"[{name}](https://huggingface.co/{name}) (Mistral AI)",
            f"revision `{rev}`" if rev else "pinned revision",
            info.get("victim_license") or "see its model card",
            "`bipia_textfooler`: the victim the variants were built against (no text of it is "
            "in the data)",
        ],
        [
            "[TextAttack](https://github.com/QData/TextAttack) (TextFooler recipe)",
            f"`textattack` {versions.get('textattack', '?')}",
            "MIT",
            "`bipia_textfooler`: the word-swap attack",
        ],
    ]


def _textfooler_licences(reports: dict[str, dict[str, Any]], included: bool) -> list[str]:
    if not included:
        return ["- **TextAttack** (MIT) is used for the TextFooler config (added in v1)."]
    info = (reports.get("p6") or {}).get("info", {})
    victim = info.get("victim", "").partition("@")[0]
    return [
        "- **TextAttack** (MIT): the TextFooler recipe that built `bipia_textfooler`.",
        f"- **{victim or 'Shieldstral (victim model)'}**: "
        f"{info.get('victim_license') or 'licence as stated on its model card'}. It was only "
        "queried; none of its weights or outputs are in the data.",
    ]


def _textfooler_section(cfg: Config, reports: dict[str, dict[str, Any]]) -> list[str]:
    p6 = reports.get("p6")
    if not p6:
        return []
    info, stats = p6.get("info", {}), p6.get("stats", {})
    c = info.get("constraints", {})
    tf = cfg.textfooler
    name, _, rev = info.get("victim", "").partition("@")
    rows = []
    for key, st in sorted(stats.get("by_bucket", {}).items()):
        scen, lab = key.split("/")
        rows.append(
            [
                scen,
                "attacked (1)" if lab == "1" else "benign (0)",
                f"{st['processed']:,}",
                f"{st['skipped']:,}",
                f"{st['succeeded']:,}",
                f"{st['failed']:,}",
                f"{st['kept']:,}",
            ]
        )
    allst = stats.get("all", {})
    ok, fail = allst.get("succeeded", 0), allst.get("failed", 0)
    cap = c.get("max_span_fraction", tf.max_span_fraction)
    return [
        "## How `bipia_textfooler` was built",
        "",
        "Adversarial variants of `bipia` test samples: TextFooler swaps words for synonyms until "
        "the victim changes its prediction. An attacked sample is made to look clean, a clean "
        "sample is made to look like an injection.",
        "",
        f"- **Victim:** base [{name}](https://huggingface.co/{name}) at revision `{rev}`. "
        f"Licence: {info.get('victim_license') or 'see its model card'}. Input: the fixed "
        "instruct, the sample's query and the whole document, through its own chat template. "
        "The score is p(yes) from the two answer-token logits; a malicious variant succeeds "
        f"below {tf.threshold}, a benign one flips at or above it.",
        "- **Attack:** TextAttack's TextFooler recipe (MIT). Only words inside the inserted text "
        "may change: the attack for attacked samples, the benign text for clean ones. For code, "
        "only the introductory sentence changes and the whole code block is protected. Emails, "
        "URLs, identifiers, inline code, numbers and named entities are protected "
        "(`protected_tokens`).",
        "- **Only variants that fooled the victim are kept.** A variant is also checked by "
        "rules (only word swaps, caps, protected tokens unchanged, nothing else in the document "
        "changed) and by an LLM judge: an attacked variant must still be judged an injection, "
        "a clean one must still be judged clean. The judge is a local model "
        f"(`{info.get('judge', tf.judge_model)}`), different from the victim, and never writes "
        "text.",
        f"- **Sizes:** target {tf.targets} kept malicious variants per scenario (floor "
        f"{tf.floor_per_scenario}); the same number of plain benign twins (`control`, copied "
        "unchanged) and of perturbed benign twins (attacked the same way, kept whether or not "
        "they fooled the victim). `p_yes_base` is the victim's score on each row.",
        "",
        "### Constraint values",
        "",
        _table(
            ["setting", "value"],
            [
                ["recipe", "TextAttack `TextFoolerJin2019`"],
                ["synonym candidates per word", c.get("max_candidates")],
                ["word-embedding cosine similarity", f">= {c.get('min_cos_sim')}"],
                [
                    "sentence similarity (Universal Sentence Encoder, angular)",
                    f">= {c.get('use_threshold')}, window {c.get('use_window')}",
                ],
                ["maximum fraction of words changed", f"{cap:.0%} of the inserted text"],
                ["search / goal", f"{c.get('search')} / {c.get('goal')}"],
                ["victim prompt", "instruct + query + whole document, no windows"],
            ],
        ),
        "",
        "### Attempts",
        "",
        _table(
            [
                "scenario",
                "class",
                "attempted",
                "skipped (victim already wrong)",
                "fooled the victim",
                "failed",
                "kept",
            ],
            rows,
        ),
        "",
        f"Attack success rate against the victim: {_pct(ok, ok + fail)} (fooled / (fooled + "
        "failed)). Variants that fooled the victim but failed the rules or the judge are not "
        "kept, so *fooled* can exceed *kept*.",
        "",
    ]


def build_card(
    cfg: Config,
    configs: list[str],
    reports: dict[str, dict[str, Any]],
    stats: dict[str, Any],
    version: str,
) -> str:
    """The dataset card (README.md): short, with sources, split choices and how it was built.
    Every number comes from the reports, the config or the files."""
    p1, p3, p4, p5 = reports["p1"], reports["p3"], reports["p4"], reports["p5"]
    gen, judge = p3.get("generator", {}), p3.get("judge", {})
    missing = [c for c in cfg.dataset.configs if c not in configs]
    docs = p1["p1_docs"]
    by_split = p4["p4_bipia"]["by_split"]
    share = cfg.p3.hard_negatives.share

    out: list[str] = [_front_matter(cfg, configs)]
    out += [
        f"# {cfg.dataset.pretty_name}",
        "",
        "Tool outputs for training and benchmarking guards against **indirect prompt "
        "injection**: an instruction hidden in a document that an AI assistant reads. Every "
        "attacked sample (label 1) has a benign *twin* (label 0): the same document and query, "
        "with a harmless text inserted at the same place instead of the attack. Only the "
        "inserted text differs, so a classifier cannot learn shortcuts from the document, the "
        "query or the position.",
        "",
        f"**Version `{version}`.** Configs: "
        + ", ".join(f"`{c}`" for c in configs)
        + "."
        + (
            " Not yet included: "
            + ", ".join(f"`{c}`" for c in missing)
            + " (TextFooler variants, added in v1)."
            if missing
            else ""
        ),
        "",
        "## Load",
        "",
        "```python",
        "from datasets import load_dataset",
        "",
    ]
    for c in configs:
        for s in SPLITS[c]:
            out.append(
                f'ds = load_dataset("{cfg.dataset.repo_id}", "{c}", split="{s}", '
                f'revision="{version}")'
            )
    out += ["```", "", "The repository is private: log in with a token that can read it.", ""]

    # Sources
    out += [
        "## Sources",
        "",
        _table(
            ["source", "used for", "licence"],
            [
                [
                    f"[BIPIA]({cfg.bipia.repo_url}) (Microsoft), commit "
                    f"`{p1.get('repo_commit', cfg.bipia.repo_commit)[:7]}`",
                    "`bipia`: the documents (emails, tables, code answers), their questions and "
                    "the 250 attacks",
                    "MIT; table and code contexts CC BY-SA 4.0",
                ],
                [
                    f"[Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1]"
                    f"(https://huggingface.co/datasets/{cfg.nemotron.repo_id}) (NVIDIA), "
                    f"revision `{p5.get('revision', cfg.nemotron.revision)[:7]}`",
                    "`nemotron`: records with an injection, and the same records without it",
                    "CC BY 4.0",
                ],
                [
                    f"Mistral Small (`{gen.get('id', cfg.p3.generator.model)}`) and Mistral "
                    f"Medium (`{judge.get('id', cfg.benign_check.judge_model)}`)",
                    "`bipia`: the synthetic benign texts of the negatives (written by Small, "
                    "judged by Medium)",
                    "Apache 2.0 (Small)",
                ],
                *(_textfooler_sources(reports) if "bipia_textfooler" in configs else []),
            ],
        ),
        "",
    ]

    # Splits
    out += ["## Splits", ""]
    if "bipia" in configs:
        total_docs = docs["total"]
        per_doc = {
            "train": f"{cfg.bipia.n_per_doc}, sampled",
            "validation": f"{cfg.bipia.n_per_doc}, sampled",
            "test": "every test attack (75 text, 50 code)",
        }
        rows = []
        for s in SPLITS["bipia"]:
            sc = by_split[s]["scenario"]
            rows.append(
                [
                    s,
                    f"{docs['split'][s]:,}",
                    per_doc[s],
                    f"{p4['split'][s]:,}",
                    *(f"{sc.get(k, 0):,}" for k in ("email", "table", "code")),
                ]
            )
        out += [
            "### `bipia`",
            "",
            f"- **Split by document.** The {total_docs:,} BIPIA documents are divided between "
            "train, validation and test, so no document is in more than one split. Test is "
            "BIPIA's official test documents plus "
            f"{docs.get('tables_moved_to_test', cfg.bipia.tables_moved_to_test)} tables moved "
            "from train (to have enough tables); about 10% of each scenario is held out for "
            "validation.",
            "- **Attacks follow the split.** Train and validation use only BIPIA's train "
            "attacks; test uses only its test attacks.",
            f"- **Sampling.** Train and validation: {cfg.bipia.n_per_doc} (attack, position) "
            "pairs sampled for each document. Test: every document meets every test attack of "
            "its scenario, at a balanced random position (start, middle or end).",
            "",
            _table(
                ["split", "documents", "attacks per document", "rows", "email", "table", "code"],
                rows,
            ),
            "",
            f"Every attacked row has its twin, so rows are half label 1 and half label 0. Test "
            f"has more rows than train although train has more documents, because test pairs "
            f"every document with every test attack (a benchmark) while train samples "
            f"{cfg.bipia.n_per_doc} per document.",
            "",
        ]
    if "nemotron" in configs:
        out += [
            "### `nemotron`",
            "",
            f"Test only: {p5['total']:,} rows ({p5['total'] // 2:,} pairs), capped at "
            f"{cfg.nemotron.goal_cap} pairs per attacker goal so that no goal dominates.",
            "",
        ]
    if "bipia_textfooler" in configs:
        out += [
            "### `bipia_textfooler`",
            "",
            "Test only: variants of `bipia` test samples (see below). Each stays in the split "
            "of its source document.",
            "",
        ]

    # How it was built
    if "bipia" in configs:
        out += [
            "## How `bipia` was built",
            "",
            "1. **Attacked samples.** An attack is inserted into a document at the start, the "
            "middle (a sentence or line boundary) or the end. The query is the document's "
            "question (email, table) or a request to fix the error (code).",
            "2. **Synthetic benign twins.** For every document and position, Mistral Small "
            "writes harmless texts that fit the document, and Mistral Medium judges each one "
            "(it must not be an injection). A twin is the same document with one of these "
            "texts inserted in place of the attack, at the same position and with the same "
            "query. About "
            f"{share:.0%} of the texts are *hard negatives*: harmless texts that sound like "
            "instructions to the human reader, so a model cannot separate the classes just "
            'because attacks use words such as "your reply" or "the following code".',
            "3. **Checks.** Before each push, the data is checked automatically (no document "
            "in two splits, every attack present, no attack text in a twin, classes 1:1, "
            "positions balanced) and two simple shortcut classifiers (document length, word "
            "counts) must stay below fixed limits.",
            "",
        ]
    if "nemotron" in configs:
        out += [
            "## How `nemotron` was built",
            "",
            "Each NVIDIA record becomes a pair: the document with its injection as released "
            "(label 1), and the same document with the injection cut out (label 0). Where a "
            "cut falls inside a sentence it is repaired by hand-checked rules. No LLM is used.",
            "",
        ]
    if "bipia_textfooler" in configs:
        out += _textfooler_section(cfg, reports)

    # Columns
    out += [
        "## Columns",
        "",
        f"Every config has the same {len(COLUMNS)} columns, in the same order. A model sees "
        "`instruct`, `query` and `document`, and predicts `answer` (`yes` = contains an "
        "injection) or `label` (1 / 0).",
        "",
        _table(
            ["column", "meaning"],
            [
                [
                    "`sample_id`, `parent_id`",
                    "unique id; on a negative, the id of its attacked twin",
                ],
                ["`source`, `split`, `source_split`", "config, split here, split in the source"],
                ["`scenario`", "BIPIA: email / table / code; Nemotron: the domain"],
                ["`instruct`, `query`", "the fixed policy text; the user's request"],
                ["`document`", "the tool output, with the attack or the benign text inside"],
                ["`label`, `answer`", "1 / `yes` = contains an injection; 0 / `no` = clean"],
                ["`attack_group`, `attack_name`, `injection_span`", "the attack (positives)"],
                ["`position`, `insert_index`", "BIPIA: where the text was inserted"],
                [
                    "`benign_id`, `benign_span`",
                    "BIPIA negatives: the benign text and its [start, end) in `document`",
                ],
                ["`protected_tokens`, `victim_model`", "TextFooler variants only"],
                [
                    "Nemotron metadata",
                    "`document_path`, `injection_vector`, `attack_category`, "
                    "`goal_group`, `wrapper`, `wrapper_outside_span`, `sentence_repaired`",
                ],
                [
                    "`label_provenance`, `generation_method`, `label_checked`",
                    "how the row and its label were made",
                ],
            ],
        ),
        "",
        "Columns that do not apply to a row are null.",
        "",
    ]

    # Licences
    out += [
        "## Licences and credit",
        "",
        f"Released under **{LICENCE_NAMES.get(cfg.dataset.license, cfg.dataset.license)}** "
        "(share-alike, because it contains CC BY-SA 4.0 material). The repository is private.",
        "",
        "- **BIPIA** (Microsoft): MIT. Its table and code contexts are CC BY-SA 4.0.",
        "- **Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1** (NVIDIA): CC BY 4.0, "
        "<https://creativecommons.org/licenses/by/4.0/>. Credit: NVIDIA. Changes: records "
        "reformatted to this schema, the document cut to the injection's path, a clean "
        "counterfactual added per record, records capped per attacker goal.",
        "- **Benign texts** in `bipia` negatives were generated with Mistral Small (Apache 2.0).",
        *_textfooler_licences(reports, "bipia_textfooler" in configs),
        "",
    ]

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- staging and secrets


def rows_per_doc(cfg: Config) -> dict[str, dict[str, int]]:
    """Min/max rows per BIPIA document per split (positives x 2), from P2's interim files."""
    out: dict[str, dict[str, int]] = {}
    for split in SPLITS["bipia"]:
        path = cfg.paths.interim / f"bipia_positives_{split}.parquet"
        if not path.is_file():
            return {}
        c = Counter(pq.read_table(path, columns=["doc_id"]).column("doc_id").to_pylist())
        vals = [2 * v for v in c.values()]
        out[split] = {"min": min(vals), "max": max(vals)}
    return out


def stage(
    cfg: Config, version: str, card: str, out_dir: Path, card_only: bool = False
) -> list[Path]:
    """Copy the parquet files this version adds, and write README.md, into a fresh staging
    folder. v1 stages only `bipia_textfooler` (+ the card): v0 files are never uploaded again."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    files: list[Path] = []
    for config in [] if card_only else new_configs(version):
        for split in SPLITS[config]:
            dst = out_dir / config / f"{split}.parquet"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(parquet_path(cfg, config, split), dst)
            files.append(dst)
    out_dir.mkdir(parents=True, exist_ok=True)
    readme = out_dir / "README.md"
    readme.write_text(card, encoding="utf-8", newline="\n")
    files.append(readme)
    return files


def _known_secrets() -> list[bytes]:
    values: list[bytes] = []
    for name in ("HF_TOKEN", "MISTRAL_API_KEY"):
        try:
            v = require_secret(name)  # type: ignore[arg-type]
        except RuntimeError:
            continue
        if len(v) >= 8:
            values.append(v.encode())
    return values


def scan_secrets(files: list[Path], secrets: list[bytes] | None = None) -> dict[str, list[str]]:
    """Files that contain a secret-looking string or a known secret value. Values are never kept."""
    secrets = _known_secrets() if secrets is None else secrets
    hits: dict[str, list[str]] = {}
    for f in files:
        data = f.read_bytes()
        found = [name for name, rx in SECRET_PATTERNS.items() if rx.search(data)]
        if any(s in data for s in secrets):
            found.append("known_secret_value")
        if found:
            hits[str(f)] = found
    return hits


# ---------------------------------------------------------------- push and verify


def push_card(cfg: Config, out_dir: Path, version: str, api: Any = None) -> dict[str, Any]:
    """Upload ONLY README.md as a new commit on the main branch. No data file is sent and no tag
    is created or moved: the tag of `version` keeps pointing at the commit it was pushed at, so
    `revision=version` still loads exactly the same data."""
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi(token=require_secret("HF_TOKEN"))
    repo = cfg.dataset.repo_id
    if not getattr(api.repo_info(repo, repo_type="dataset"), "private", False):
        raise RuntimeError(f"{repo} is not private: refusing to push.")
    tags = {t.name for t in getattr(api.list_repo_refs(repo, repo_type="dataset"), "tags", [])}
    if version not in tags:
        raise RuntimeError(f"tag {version!r} does not exist on {repo}: push the version first.")
    commit = api.upload_file(
        repo_id=repo,
        repo_type="dataset",
        path_or_fileobj=str(out_dir / "README.md"),
        path_in_repo="README.md",
        commit_message=f"Update the dataset card ({version} data unchanged)",
    )
    return {
        "commit": getattr(commit, "oid", None),
        "commit_url": getattr(commit, "commit_url", None),
        "files": ["README.md"],
        "tag_moved": False,
    }


def push(cfg: Config, out_dir: Path, version: str, api: Any = None) -> dict[str, Any]:
    """Upload the staged folder to the private repo and tag the commit with the version."""
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi(token=require_secret("HF_TOKEN"))
    repo = cfg.dataset.repo_id
    info = api.repo_info(repo, repo_type="dataset")
    if not getattr(info, "private", False):
        raise RuntimeError(f"{repo} is not private: refusing to push.")
    tags = {t.name for t in getattr(api.list_repo_refs(repo, repo_type="dataset"), "tags", [])}
    if version in tags:
        raise RuntimeError(f"tag {version!r} already exists on {repo}: a version is pushed once.")
    if version != "v0" and "v0" not in tags:
        raise RuntimeError(f"{version} adds to v0, but tag 'v0' does not exist on {repo}.")
    commit = api.upload_folder(
        repo_id=repo,
        repo_type="dataset",
        folder_path=str(out_dir),
        commit_message=f"{version}: add " + ", ".join(new_configs(version)),
    )
    oid = getattr(commit, "oid", None)
    api.create_tag(repo, repo_type="dataset", tag=version, revision=oid, tag_message=version)
    return {"commit": oid, "commit_url": getattr(commit, "commit_url", None), "tag": version}


VERIFY_SCRIPT = """
import json, sys
from datasets import load_dataset
repo, rev, pairs = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
out = {}
for config, split in pairs:
    ds = load_dataset(repo, config, split=split, revision=rev)
    out[f"{config}/{split}"] = {"rows": ds.num_rows, "columns": ds.column_names}
print("P10_VERIFY " + json.dumps(out))
"""


def compare_files(dir_a: Path, dir_b: Path, rel_paths: list[str]) -> list[str]:
    """Relative paths that are missing or whose sha256 differs between two folders."""
    bad = []
    for rel in rel_paths:
        a, b = dir_a / rel, dir_b / rel
        if not a.is_file() or not b.is_file():
            bad.append(f"{rel}: missing in {'first' if not a.is_file() else 'second'}")
        elif (
            hashlib.sha256(a.read_bytes()).hexdigest() != hashlib.sha256(b.read_bytes()).hexdigest()
        ):
            bad.append(f"{rel}: content differs")
    return bad


def verify(cfg: Config, version: str) -> dict[str, Any]:
    """Load every config/split at the version's tag from the Hub in a clean uv environment and
    compare with the reports. From v1 on, also load v0 again and check that it is unchanged."""
    token = require_secret("HF_TOKEN")
    reports = load_reports(cfg, version)
    env = {**os.environ, "HF_TOKEN": token, "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
    # A fresh, SHORT cache folder: nothing is read from earlier downloads, and the `datasets`
    # lock-file name embeds the cache path (Windows fails above 260 characters).
    work = Path(tempfile.mkdtemp(prefix="p10v"))
    try:
        return _verify_in(cfg, version, token, reports, env, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _load_at(cfg: Config, rev: str, configs: list[str], token: str, env: dict, work: Path) -> dict:
    env = {**env, "HF_HOME": str(work / f"c{rev}")}
    pairs = [[c, s] for c in configs for s in SPLITS[c]]
    proc = subprocess.run(
        [
            "uv",
            "run",
            "--isolated",
            "--no-project",
            "--with",
            "datasets",
            "python",
            "-c",
            VERIFY_SCRIPT,
            cfg.dataset.repo_id,
            rev,
            json.dumps(pairs),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        tail = proc.stderr.replace(token, "***")[-3000:]
        raise RuntimeError(f"load_dataset failed at revision {rev}:\n{tail}")
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("P10_VERIFY "))
    return json.loads(line.removeprefix("P10_VERIFY "))


def _count_errors(loaded: dict, configs: list[str], reports: dict, rev: str) -> list[str]:
    errors = []
    for c in configs:
        exp = expected_rows(reports, c)
        for s in SPLITS[c]:
            got = loaded.get(f"{c}/{s}", {})
            if got.get("rows") != exp.get(s):
                errors.append(
                    f"{rev} {c}/{s}: loaded {got.get('rows')} rows, reports say {exp.get(s)}"
                )
            if tuple(got.get("columns", ())) != columns_for(c):
                errors.append(f"{rev} {c}/{s}: loaded columns differ from the schema")
    return errors


def _verify_in(
    cfg: Config,
    version: str,
    token: str,
    reports: dict[str, dict[str, Any]],
    env: dict[str, str],
    work: Path,
) -> dict[str, Any]:
    from huggingface_hub import HfApi

    configs = configs_of(version)
    loaded = _load_at(cfg, version, configs, token, env, work)
    errors = _count_errors(loaded, configs, reports, version)

    # Secret check on the remote files (README and parquet) at the tag.
    api = HfApi(token=token)
    local = Path(
        api.snapshot_download(
            cfg.dataset.repo_id, repo_type="dataset", revision=version, local_dir=str(work / "f")
        )
    )
    remote_files = [p for p in local.rglob("*") if p.is_file() and ".cache" not in p.parts]
    hits = scan_secrets(remote_files, [token.encode(), *_known_secrets()])
    if hits:
        errors.append(f"secret-looking content in remote files: {sorted(hits)}")
    info = api.repo_info(cfg.dataset.repo_id, repo_type="dataset")
    if not info.private:
        errors.append("the repository is not private")

    v0_unchanged = None
    if version != "v0":
        v0_configs = configs_of("v0")
        loaded_v0 = _load_at(cfg, "v0", v0_configs, token, env, work)
        errors += _count_errors(loaded_v0, v0_configs, load_reports(cfg, "v0"), "v0")
        local0 = Path(
            api.snapshot_download(
                cfg.dataset.repo_id, repo_type="dataset", revision="v0", local_dir=str(work / "f0")
            )
        )
        rels = [f"{c}/{s}.parquet" for c in v0_configs for s in SPLITS[c]]
        diff = compare_files(local0, local, rels)
        errors += [f"v0 file changed in {version}: {d}" for d in diff]
        v0_unchanged = {"loaded": loaded_v0, "files_compared": rels, "differences": diff}
    return {
        "tag": version,
        "loaded": loaded,
        "remote_files": sorted(str(p.relative_to(local)).replace("\\", "/") for p in remote_files),
        "private": bool(info.private),
        "v0_unchanged": v0_unchanged,
        "errors": errors,
    }


# ---------------------------------------------------------------- report


def card_copy_name(version: str) -> str:
    return f"p10_dataset_card_{version}.md"


def _rel(path: str | Path) -> str:
    """A path relative to the repository root when possible (no user folders in reports)."""
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _report(cfg: Config, version: str, payload: dict[str, Any]) -> Path:
    name = f"{REPORT_NAME}_{version}"
    prev_path = report_path(cfg, f"{name}.json")
    prev = json.loads(prev_path.read_text(encoding="utf-8")) if prev_path.is_file() else {}
    # Keep earlier push / verify results when a later dry-run or verify rewrites the report.
    data = prev | payload
    prev_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    gate = data.get("gate", {})
    lines = [f"# Report: {name}", ""]
    lines += [
        "## Run",
        "",
        f"- Repository: `{cfg.dataset.repo_id}` (private: {cfg.dataset.private})",
        f"- Version / tag: `{version}`",
        f"- Configs in this version: {', '.join(f'`{c}`' for c in data.get('configs', []))}",
        f"- Uploaded by this push: {', '.join(f'`{c}`' for c in data.get('uploads', []))} "
        "and the card",
        f"- Last action: {data.get('action')}",
        "",
        "## P7 gate",
        "",
        f"- P7 {version}: **{gate.get('status', 'n/a')}**"
        + (f" ({gate['reason']})" if gate.get("reason") else "")
        + (". A push is allowed." if gate.get("ok") else ". A push is blocked."),
        "",
        "## Preflight",
        "",
        f"Step reports have no failed checks; every file has the {len(COLUMNS)} schema columns "
        "in order, the reported row count, classes 1:1, one split and source value, and unique "
        "`sample_id`s.",
        "",
        _table(
            ["file", "rows", "label 1", "label 0", "MB"],
            [
                [
                    k,
                    f"{v['rows']:,}",
                    f"{v['label'].get('1', 0):,}",
                    f"{v['label'].get('0', 0):,}",
                    f"{v['bytes'] / 1e6:.1f}",
                ]
                for k, v in data.get("files", {}).items()
            ],
        ),
        "",
        "## Dataset card",
        "",
        f"`README.md`, sha256 `{data.get('card_sha256', '')[:16]}…`, staged in "
        f"`{_rel(data.get('stage_dir', ''))}`; a copy is kept in "
        f"`reports/p10/{card_copy_name(version)}`.",
        "",
        "## Secret scan",
        "",
        "Staged files: "
        + (
            "**none found**"
            if not data.get("secrets_staged")
            else f"**FOUND** in {data['secrets_staged']}"
        )
        + " (token patterns and the actual `HF_TOKEN` / `MISTRAL_API_KEY` values).",
        "",
    ]
    if data.get("push"):
        p = data["push"]
        lines += [
            "## Push",
            "",
            f"- Commit: `{p.get('commit')}`",
            f"- Tag: `{p.get('tag')}`",
            f"- URL: {p.get('commit_url')}",
            "",
        ]
    if data.get("card_push"):
        c = data["card_push"]
        lines += [
            "## Card update",
            "",
            "Only `README.md` was uploaded, as a new commit on `main`. No data file was sent and "
            f"the tag `{version}` was not moved.",
            "",
            f"- Commit: `{c.get('commit')}`",
            f"- URL: {c.get('commit_url')}",
            "",
        ]
    if data.get("verify"):
        v = data["verify"]
        lines += [
            f"## Verify (clean environment, `load_dataset` at revision `{version}`)",
            "",
            _table(
                ["config/split", "rows loaded"],
                [[k, f"{x['rows']:,}"] for k, x in v.get("loaded", {}).items()],
            ),
            "",
            f"- Repository private: {v.get('private')}",
            f"- Remote files: {', '.join(f'`{f}`' for f in v.get('remote_files', []))}",
            "- Secret scan of the remote files: "
            + ("none found" if not any("secret" in e for e in v.get("errors", [])) else "FOUND"),
        ]
        u = v.get("v0_unchanged")
        if u:
            lines += [
                "- v0 loaded again at revision `v0`: "
                + ", ".join(f"{k} {x['rows']:,}" for k, x in u["loaded"].items()),
                f"- v0 files compared between `v0` and `{version}` (sha256): "
                f"{len(u['files_compared'])} files, "
                + ("**unchanged**" if not u["differences"] else f"**DIFFER**: {u['differences']}"),
            ]
        lines += [
            "- Result: "
            + ("**PASSED**" if not v.get("errors") else "**FAILED**: " + "; ".join(v["errors"])),
            "",
        ]
    lines += [
        "## Deviations from the brief",
        "",
        "- **Repository name:** `SAIL-Reply/ipi-twins` instead of `<org>/ipi-shieldstral` "
        "(team decision).",
        "- **Verify:** the clean environment is `uv run --isolated --no-project --with datasets` "
        "with a fresh Hugging Face cache.",
        "- **Earlier push:** an early `v0.1` (29 columns, original pool) was pushed before the "
        "version scheme existed; its record is in `reports/archive/p10/v0.1/`. `v0` replaces "
        "it in `main`; the `v0.1` tag keeps it reachable.",
        "",
    ]
    path = report_path(cfg, f"{name}.md")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- entry points


def run(
    cfg: Config | None = None,
    *,
    version: str = "v0",
    do_push: bool = False,
    card_only: bool = False,
    api: Any = None,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    configs = configs_of(version)
    reports = load_reports(cfg, version)
    stats = preflight(cfg, configs, reports)  # all configs of the version, v0 files included
    reports["rows_per_doc"] = rows_per_doc(cfg)
    reports["hn_share"] = hn_share(cfg)
    card = build_card(cfg, configs, reports, stats, version)
    out_dir = stage_dir(cfg)
    files = stage(cfg, version, card, out_dir, card_only=card_only)
    hits = scan_secrets(files)
    report_path(cfg, card_copy_name(version)).write_text(card, encoding="utf-8", newline="\n")
    gate = p7_gate(cfg, version, configs)
    payload: dict[str, Any] = {
        "action": ("card update" if card_only else "push") if do_push else "dry-run",
        "version": version,
        "configs": configs,
        "uploads": [] if card_only else new_configs(version),
        "files": stats,
        "gate": gate,
        "card_sha256": hashlib.sha256(card.encode()).hexdigest(),
        "stage_dir": _rel(out_dir),
        "secrets_staged": sorted(hits),
    }
    if hits:
        _report(cfg, version, payload)
        raise SecretFoundError(f"secret-looking content in staged files: {sorted(hits)}")
    if do_push:
        if not gate["ok"]:
            _report(cfg, version, payload)
            raise PushBlockedError(f"push blocked: {gate['reason']}")
        if card_only:
            payload["card_push"] = push_card(cfg, out_dir, version, api=api)
        else:
            payload["push"] = push(cfg, out_dir, version, api=api)
            payload["verify"] = None
    report = _report(cfg, version, payload)
    return {**payload, "report": report, "card": out_dir / "README.md"}


def run_verify(cfg: Config | None = None, version: str = "v0") -> dict[str, Any]:
    cfg = cfg or load_config()
    result = verify(cfg, version)
    report = _report(cfg, version, {"action": "verify", "verify": result})
    if result["errors"]:
        raise CountMismatchError("P10 verify failed:\n  " + "\n  ".join(result["errors"]))
    return {**result, "report": report}
