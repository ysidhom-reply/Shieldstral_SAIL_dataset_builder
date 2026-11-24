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
from dataset_builder.schema import COLUMNS

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
            if cols != COLUMNS:
                extra = sorted(set(cols) - set(COLUMNS))
                missing = sorted(set(COLUMNS) - set(cols))
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


def filter_rates(p3: dict[str, Any]) -> list[dict[str, Any]]:
    """Texts checked / rejected per P3 filter, from the report's `reach` and `rejected` tables
    (keys `scenario|position|filter`)."""
    from dataset_builder.steps.p3_benign_pool import FILTERS

    rows = []
    for f, name in FILTERS.items():
        checked = sum(v for k, v in p3.get("reach", {}).items() if k.rsplit("|", 1)[-1] == str(f))
        rejected = sum(
            v for k, v in p3.get("rejected", {}).items() if k.rsplit("|", 1)[-1] == str(f)
        )
        rows.append({"filter": f, "name": name, "checked": checked, "rejected": rejected})
    return rows


LICENCE_NAMES = {"cc-by-sa-4.0": "CC BY-SA 4.0", "cc-by-4.0": "CC BY 4.0", "mit": "MIT"}


def _model(info: dict[str, Any], default: str) -> str:
    return f"`{info.get('id', default)}` (requested `{info.get('requested', default)}`)"


def _prompts(cfg: Config) -> dict[str, Any]:
    return yaml.safe_load(cfg.p3.prompts.read_text(encoding="utf-8"))


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


def _hard_negative_section(
    cfg: Config, reports: dict[str, dict[str, Any]], prompts: dict[str, Any]
) -> list[str]:
    hn = reports.get("p3hn")
    if not hn:
        return []
    c = hn.get("counts", {})
    hcfg = cfg.p3.hard_negatives
    out = [
        "## Hard negatives",
        "",
        f"{hcfg.share:.0%} of every benign pool is a *hard negative*: a harmless text that "
        'sounds like an instruction to the human reader of the document ("Please send your '
        'reply by Friday", "You can add the following snippet to your script ..."), using '
        "the vocabulary attacks use. Why: a bag-of-words baseline separated the classes on "
        "wording alone when benign texts never used that vocabulary.",
        "",
        f"- **Texts:** {c.get('new', 0):,} hard negatives replaced the same number of the "
        "original texts (the ones with the most over-represented words first). Every pool keeps "
        "its size.",
        f"- **Prompts:** `{hcfg.prompt_version}` (A-hn for email and table, B-hn for code; below). "
        "Email texts address the recipient of the email, table texts are reading notes for the "
        "reader, code texts are suggestions about the reader's own code with harmless code.",
        f"- **Judge:** `{hcfg.judge_prompt_version}` (one text per call for code, "
        f"`{hcfg.judge_batch_prompt_version}` for email and table). A suggestion to the reader "
        "about their own code, or an email request to the recipient, is not an injection; code "
        "that does something unrequested still is, however it is phrased.",
    ]
    jc = hn.get("judge_control") or {}
    if jc:
        out.append(
            f"- **Judge positive control:** {jc.get('flagged')} of {jc.get('n')} real BIPIA "
            "attacks were flagged."
        )
    out += [
        "- **Filters:** a narrower profile than the original texts: assistant wording "
        "(assistant, chatbot, ignore, disregard, system prompt, pretend, as an AI) is still "
        "forbidden; `your reply/answer/response` and reader-directed code intros are allowed; "
        "`requests.get` with a timeout on `example.com`, `subprocess.run` with fixed arguments "
        "and local file reads are allowed in code; sending data, credentials, environment "
        "variables, deletion, `shell=True`, sockets and system fingerprinting stay forbidden. "
        "The overlap ban with BIPIA attacks and the duplicate check are unchanged.",
        "",
    ]
    rows = filter_rates(hn)
    if rows:
        out += [
            _table(
                ["filter", "texts checked", "rejected", "rejection rate"],
                [
                    [
                        r["name"],
                        f"{r['checked']:,}",
                        f"{r['rejected']:,}",
                        _pct(r["rejected"], r["checked"]),
                    ]
                    for r in rows
                ],
            ),
            "",
        ]
    share = reports.get("hn_share") or {}
    if share:
        out += [
            "Share of `bipia` negatives that use a hard negative:",
            "",
            _table(
                ["split", "email", "table", "code"],
                [
                    [sp, *(_pct(v.get(sc, 0), 1) for sc in ("email", "table", "code"))]
                    for sp, v in share.items()
                ],
            ),
            "",
            "P4 chooses, among the least-used texts within the length window, the one with the "
            "fewest over-represented benign words, so the share in train and validation depends "
            "on that choice (see Known limits).",
            "",
        ]
    for key, title in (
        ("prompt_a_hn", "Prompt A-hn (email, table)"),
        ("prompt_b_hn", "Prompt B-hn (code)"),
        ("judge_v4", "Judge for hard negatives (one text per call)"),
    ):
        pr = prompts.get(key)
        if not pr:
            continue
        out += [f"<details><summary>{title}</summary>", ""]
        for role in ("system", "user"):
            if role in pr:
                out += [f"**{role}**", "", "```text", str(pr[role]).rstrip(), "```", ""]
        out += ["</details>", ""]
    return out


def _hard_negative_limits(reports: dict[str, dict[str, Any]]) -> list[str]:
    share = reports.get("hn_share") or {}
    tr, te = share.get("train", {}), share.get("test", {})
    if not tr or not te:
        return []
    return [
        "- **Hard negatives in train vs test:** in test every pool text is used once, so "
        + ", ".join(f"{sc} {te.get(sc, 0):.0%}" for sc in ("email", "table", "code"))
        + " of the negatives are hard negatives; in train "
        + ", ".join(f"{sc} {tr.get(sc, 0):.0%}" for sc in ("email", "table", "code"))
        + ", because P4 prefers texts without over-used words.",
    ]


def _p7_section(cfg: Config, reports: dict[str, dict[str, Any]], version: str) -> list[str]:
    p7 = reports.get("p7")
    if not p7:
        return []
    lim = p7.get("limits", {})
    base = p7.get("baselines", {})
    rows = []
    for name in ("length", "tfidf"):
        for ev, groups in base.get(name, {}).items():
            for g in ("email", "table", "code", "all"):
                m = groups.get(g)
                if not m or m.get("auc") is None:
                    continue
                rows.append([name, ev, g, f"{m['n']:,}", f"{m['auc']:.3f}", f"{m['recall']:.1%}"])
    out = [
        "## Checks and shortcut baselines",
        "",
        f"Before this version was pushed, the data passed an automatic check ({version}): the "
        "splits share no document, no train attack text is in test, every positive contains "
        "its attack, no negative contains attack text, classes are 1:1 per scenario, positions "
        "are balanced, and removing the inserted text from both members of a pair gives the "
        "same clean document. Two throwaway classifiers were trained on `bipia` train only, to "
        "test the data for shortcuts: logistic regression on document length, and TF-IDF "
        f"(word 1-2-grams) with logistic regression. Limits: AUC {lim.get('length_auc_stop')} "
        f"(length) and {lim.get('tfidf_auc_stop')} (TF-IDF); a result above a limit blocks the "
        "push. Recall is at 1% false-positive rate.",
        "",
        _table(["baseline", "evaluated on", "scenario", "rows", "AUC", "recall at 1% FPR"], rows),
        "",
    ]
    nl = (p7.get("nemotron_length") or {}).get("all")
    if nl:
        out += [
            f"On `nemotron` the raw document length alone gives AUC {nl['auc']:.3f} (the positive "
            f"is longer in {nl['positive_longer']:.0%} of pairs): the negative is the positive "
            "with the injection cut out. It is reported, not a failure.",
            "",
        ]
    return out


def _textfooler_sources(reports: dict[str, dict[str, Any]]) -> list[list[str]]:
    info = (reports.get("p6") or {}).get("info", {})
    versions = info.get("versions", {})
    name, _, rev = info.get("victim", "").partition("@")
    return [
        [
            f"[{name}](https://huggingface.co/{name}) (ProtectAI)",
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
        f"- **{victim or 'ProtectAI victim model'}**: "
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
        f"- **Victim:** [{name}](https://huggingface.co/{name}) at revision `{rev}`, on CPU, "
        f"licence: {info.get('victim_license') or 'see its model card'}. Input: the document "
        f"only. Documents longer than {tf.window} tokens use a sliding window ({tf.window} "
        f"tokens, stride {tf.stride}); the score is the maximum injection probability over the "
        "windows.",
        "- **Attack:** TextAttack's TextFooler recipe (MIT). Only words inside the inserted text "
        "may change: the attack for attacked samples, the benign text for clean ones. For code, "
        "only the introductory sentence changes and the whole code block is protected. Emails, "
        "URLs, identifiers, inline code, numbers and named entities are protected "
        "(`protected_tokens`).",
        "- **Only variants that fooled the victim are kept.** A variant is also checked by "
        "rules (only word swaps, caps, protected tokens unchanged, nothing else in the document "
        "changed) and by an LLM judge: an attacked variant must still be judged an injection, "
        "a clean one must still be judged clean. The judge never writes text.",
        f"- **Sizes:** classes are equal in every scenario; target {tf.targets} kept variants "
        "per class and scenario.",
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
                ["window rule", f"{tf.window} tokens, stride {tf.stride}, maximum score"],
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
    """The dataset card (README.md). Every number comes from the reports, config or files."""
    p1, p3, p4, p5 = reports["p1"], reports["p3"], reports["p4"], reports["p5"]
    prompts = _prompts(cfg)
    gen, judge = p3.get("generator", {}), p3.get("judge", {})
    missing = [c for c in cfg.dataset.configs if c not in configs]
    test_total = p4["split"]["test"]
    test_tables = p4["p4_bipia"]["by_split"]["test"]["scenario"]["table"]
    pairs, matched = p4.get("pairs", 0), p4.get("length_matched", 0)
    rows_per_doc = reports.get("rows_per_doc", {})
    tag = version

    out: list[str] = [_front_matter(cfg, configs)]
    out += [
        f"# {cfg.dataset.pretty_name}",
        "",
        "Pairs of tool outputs for training and benchmarking guards against **indirect prompt "
        "injection** (IPI). Every attacked sample (label 1) has a benign *twin* (label 0): the "
        "same document and query, with a harmless text inserted at the same place instead of "
        "the attack. Only the inserted text differs, so a classifier cannot learn shortcuts "
        "from the document, the query or the position.",
        "",
        f"**Version `{tag}`.** Configs in this version: "
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
                f'ds = load_dataset("{cfg.dataset.repo_id}", "{c}", split="{s}", revision="{tag}")'
            )
    out += ["```", "", "The repository is private: log in with a token that can read it.", ""]

    # Sizes and balance
    out += ["## Splits and class balance", ""]
    rows = []
    for key, st in stats.items():
        c, s = key.split("/")
        lab = st["label"]
        rows.append(
            [f"`{c}`", s, f"{st['rows']:,}", f"{lab.get('1', 0):,}", f"{lab.get('0', 0):,}"]
        )
    out += [_table(["config", "split", "rows", "label 1", "label 0"], rows), ""]
    if "bipia" in configs:
        by_split = p4["p4_bipia"]["by_split"]
        out += [
            "`bipia` rows per scenario:",
            "",
            _table(
                ["split", "email", "table", "code"],
                [
                    [
                        s,
                        *(
                            f"{by_split[s]['scenario'].get(sc, 0):,}"
                            for sc in ("email", "table", "code")
                        ),
                    ]
                    for s in SPLITS["bipia"]
                ],
            ),
            "",
            "Insertion positions (start / middle / end) are balanced within every split and "
            "scenario, for both classes.",
            "",
        ]

    # Schema
    out += [
        "## Columns",
        "",
        f"Every config has the same {len(COLUMNS)} columns, in the same order. A model sees "
        "`instruct`, "
        "`query` and `document`, and predicts `answer` (`yes` = contains an injection).",
        "",
        _table(
            ["column", "meaning"],
            [
                ["`sample_id`", "unique id"],
                ["`parent_id`", "on a negative: the `sample_id` of its attacked twin"],
                [
                    "`source`, `split`, `source_split`",
                    "config, split here, split in the source dataset",
                ],
                ["`scenario`", "BIPIA: email / table / code; Nemotron: the domain"],
                ["`instruct`", "the fixed policy text (identical in every row)"],
                ["`query`", "the user's request, in a fixed template"],
                ["`document`", "the tool output, with the attack or the benign text inside"],
                ["`label`, `answer`", "1 / `yes` = contains an injection; 0 / `no` = clean"],
                ["`label_provenance`", "`construction` or `injection_metadata`"],
                [
                    "`generation_method`",
                    "how the row was made (`sampled_insertion`, `llm_benign_insertion`, "
                    "`original`, `counterfactual`)",
                ],
                ["`attack_group`, `attack_name`", "BIPIA attack type and attack (positives)"],
                ["`position`, `insert_index`", "BIPIA: where the text was inserted"],
                ["`injection_span`", "the inserted attack (positives)"],
                ["`protected_tokens`", "tokens TextFooler must not change (later configs)"],
                [
                    "`benign_id`, `benign_span`, `label_checked`",
                    "BIPIA negatives: the pool text used; its [start, end) characters in "
                    "`document`; whether a classifier label check ran",
                ],
                [
                    "`document_path`, `injection_vector`, `attack_category`, `goal_group`, "
                    "`wrapper`, `wrapper_outside_span`, `sentence_repaired`",
                    "Nemotron metadata",
                ],
                ["`victim_model`", "TextFooler victim model (later configs)"],
            ],
        ),
        "",
        "Columns that do not apply to a row are null.",
        "",
    ]

    # Sources
    out += [
        "## Sources and versions",
        "",
        _table(
            ["source", "version", "licence", "used for"],
            [
                [
                    f"[BIPIA]({cfg.bipia.repo_url}) (Microsoft)",
                    f"commit `{p1.get('repo_commit', cfg.bipia.repo_commit)}`",
                    "MIT; table and code contexts CC BY-SA 4.0",
                    "`bipia`: documents, questions and attacks",
                ],
                [
                    f"[Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1]"
                    f"(https://huggingface.co/datasets/{cfg.nemotron.repo_id}) (NVIDIA)",
                    f"revision `{p5.get('revision', cfg.nemotron.revision)}`",
                    "CC BY 4.0",
                    "`nemotron`: records and their injections",
                ],
                [
                    f"Mistral Small (`{gen.get('id', cfg.p3.generator.model)}`)",
                    "API, pinned id",
                    "Apache 2.0",
                    "`bipia`: benign texts of the negatives",
                ],
                [
                    f"Mistral Medium (`{judge.get('id', cfg.benign_check.judge_model)}`)",
                    "API, pinned id",
                    "n/a (judge only)",
                    "`bipia`: judging the benign texts (no text of the judge is in the data)",
                ],
                *(_textfooler_sources(reports) if "bipia_textfooler" in configs else []),
            ],
        ),
        "",
    ]

    # Construction: bipia
    if "bipia" in configs:
        docs = p1["p1_docs"]
        f_rows = filter_rates(p3)
        out += [
            "## How `bipia` was built",
            "",
            f"1. **Documents (P1).** BIPIA's email, table and code documents are deduplicated "
            f"({docs['total']:,} documents: "
            + ", ".join(f"{k} {v:,}" for k, v in docs["scenario"].items())
            + "). The split is per document, so no document is in two splits: "
            + ", ".join(f"{k} {v:,}" for k, v in docs["split"].items())
            + f". BIPIA's official test documents go to test, plus "
            f"{docs.get('tables_moved_to_test', cfg.bipia.tables_moved_to_test)} tables moved "
            f"from train; about 10% of each scenario is held out for validation. "
            f"{docs.get('cross_split_emails_dropped', 0)} emails found in both official splits "
            "are dropped from train.",
            f"2. **Attacked samples (P2, label 1).** Test documents get only BIPIA's test "
            f"attacks, train and validation only the train attacks "
            f"({p1['p1_attacks']['total']} attacks). Test: every document x every test attack "
            f"(75 text attacks, 50 code attacks), at a balanced random position. "
            f"Train/validation: {cfg.bipia.n_per_doc} sampled (attack, position) pairs per "
            "document. The attack is inserted at the start, the middle (a sentence or line "
            "boundary) or the end, joined with a newline. The query is the document's "
            "question (email, table) or `Fix the bug in my code. Error: <last line of the "
            "error>` (code).",
            f"3. **Benign pool (P3).** For every document and position, Mistral Small writes "
            f"{cfg.p3.n} short texts that fit the document (25 for test email/table pools). "
            "Each text passes four filters: shape, forbidden content (instructions, "
            "attack-like wording, network or file code), near-duplicates, and an LLM judge "
            f"(Mistral Medium). {p3['total']:,} texts were kept out of "
            f"{p3.get('generated', 0):,} generated. {cfg.p3.hard_negatives.share:.0%} of "
            "every pool is then replaced by hard negatives (see *Hard negatives*).",
            f"4. **Negatives (P4, label 0).** Each positive gets one pool text of its own "
            "document and position, inserted with the same function: least used text first, "
            f"then within {cfg.p4.length_window['text']:.0%} of the attack's length "
            f"({cfg.p4.length_window['code']:.0%} for code), then the fewest over-represented "
            "benign words, then the closest length. No negative row appears twice. The "
            "negative's `benign_span` is where its text sits in `document`.",
            "",
            "### P3 generation settings",
            "",
            _table(
                ["setting", "value"],
                [
                    [
                        "generator",
                        _model(gen, cfg.p3.generator.model),
                    ],
                    [
                        "judge",
                        _model(judge, cfg.benign_check.judge_model),
                    ],
                    [
                        "temperature",
                        "one seeded value per call in [{}, {}]".format(
                            *cfg.p3.generator.temperature
                        ),
                    ],
                    ["reasoning effort", f"`{cfg.p3.generator.reasoning_effort}`"],
                    [
                        "max tokens",
                        ", ".join(f"{k} {v}" for k, v in cfg.p3.generator.max_tokens.items()),
                    ],
                    [
                        "length targets (characters)",
                        ", ".join(f"{k} {v}" for k, v in cfg.p3.lengths.items()),
                    ],
                    [
                        "size mix per pool (short/medium/long)",
                        "/".join(str(cfg.p3.mix[k]) for k in ("short", "medium", "long")),
                    ],
                    [
                        "judge batch size",
                        ", ".join(f"{k} {v}" for k, v in cfg.p3.judge_batch_size.items()) or "1",
                    ],
                    [
                        "prompt versions",
                        "`{}`, judge `{}`, batch judge `{}`".format(
                            prompts.get("prompt_version"),
                            prompts.get("judge_prompt_version"),
                            prompts.get("judge_batch_prompt_version"),
                        ),
                    ],
                    ["seed", str(cfg.seed)],
                ],
            ),
            "",
        ]
        if f_rows:
            out += [
                "### P3 rejection rate per filter (original texts)",
                "",
                _table(
                    ["filter", "texts checked", "rejected", "rejection rate"],
                    [
                        [
                            r["name"],
                            f"{r['checked']:,}",
                            f"{r['rejected']:,}",
                            _pct(r["rejected"], r["checked"]),
                        ]
                        for r in f_rows
                    ],
                ),
                "",
                "A text is counted at the first filter it fails.",
                "",
            ]
        jc = p3.get("judge_control") or {}
        if jc:
            out += [
                f"**Judge positive control:** {jc.get('flagged')} of {jc.get('n')} real BIPIA "
                "attacks, judged with every run, were flagged.",
                "",
            ]
        out += [
            "### P3 prompts (original texts)",
            "",
            "Placeholders in braces are filled per document and position.",
            "",
        ]
        for key, title in (
            ("prompt_a", "Prompt A (email, table)"),
            ("prompt_b", "Prompt B (code)"),
            ("judge", "Judge (one text per call)"),
            ("judge_batch", "Judge (batch)"),
        ):
            p = prompts.get(key)
            if not p:
                continue
            out += [f"<details><summary>{title}</summary>", ""]
            for role in ("system", "user"):
                if role in p:
                    out += [f"**{role}**", "", "```text", str(p[role]).rstrip(), "```", ""]
            out += ["</details>", ""]

    if "bipia" in configs:
        out += _hard_negative_section(cfg, reports, prompts)

    # Construction: nemotron
    if "nemotron" in configs:
        s5 = p5.get("stats", {})
        out += [
            "## How `nemotron` was built",
            "",
            f"1. **Records (P5).** {s5.get('records', 0):,} records of NVIDIA's release, file "
            f"`{cfg.nemotron.filename}`, at the pinned revision.",
            "2. **Document:** the first two levels of the path where the injection sits, "
            "serialised as JSON. The injection is left exactly as released.",
            "3. **Positive (label 1):** the record as released. **Negative (label 0):** the same "
            "document with the injection cut out; where the cut falls inside a sentence the "
            f"sentence is repaired by hand-checked rules ({s5.get('sentence_repaired', 0)} "
            "records).",
            f"4. **Goal cap:** at most {cfg.nemotron.goal_cap} pairs per attacker goal "
            f"({s5.get('before_cap', {}).get('total', 0):,} rows before the cap, "
            f"{p5['total']:,} after).",
            "5. **Review:** every cut point and the "
            f"{s5.get('hard_negatives', {}).get('after_cap', 0)} "
            "hard negatives (negatives that contain words such as 'urgent' or 'assistant') were "
            "inspected; 20 random pairs were read in full.",
            "",
            "**Changes to NVIDIA's data (CC BY 4.0):** records were reformatted to this schema, "
            "the document was cut to the injection's path, a clean counterfactual was added per "
            "record, and records were capped per attacker goal.",
            "",
        ]

    if "bipia_textfooler" in configs:
        out += _textfooler_section(cfg, reports)
    out += _p7_section(cfg, reports, version)

    # Limits
    out += ["## Known limits", ""]
    if "bipia" in configs:
        out += [
            f"- **Tables dominate `bipia` test:** {test_tables:,} of {test_total:,} test rows "
            f"({_pct(test_tables, test_total)}). Report metrics per `scenario`.",
            "- **Documents repeat:** a document appears in "
            + (
                ", ".join(
                    f"{v['min']}-{v['max']} rows in {s}"
                    if v["min"] != v["max"]
                    else f"{v['max']} rows in {s}"
                    for s, v in rows_per_doc.items()
                )
                if rows_per_doc
                else "many rows"
            )
            + " (once per attack, plus its twin). Train has more documents than test but fewer "
            "rows, because test pairs every document with every test attack.",
            "- **Attack types:** the name 'Language Translation' is used in both BIPIA's train "
            "and test attacks, with different strings (train: translate a phrase; test: answer "
            "in another language). Report test metrics per `attack_group`, with and without it.",
            f"- **Length matching:** {_pct(matched, pairs)} of pairs are within the length "
            "window; the rest (mostly long test attacks) use the closest-length pool text.",
            *_hard_negative_limits(reports),
            "- **No extra classifier label check** was run on the benign texts "
            "(`label_checked = false`); they were checked by the filters and the judge.",
        ]
    if "nemotron" in configs:
        out += [
            "- **Nemotron positives are only attacks that succeeded against one defender** "
            "in NVIDIA's release, so they are not a sample of all possible attacks. `nemotron` "
            "is a test set only. In every pair the positive is longer than the negative (the "
            "injection is cut out of it), so document length separates the two classes there: "
            "do not read length-sensitive results on `nemotron` as detection skill.",
        ]
    if "bipia_textfooler" in configs:
        out += [
            "- **`bipia_textfooler` keeps only variants that fooled ProtectAI.** It measures "
            "robustness to one attack against one victim, not the rate at which a guard is "
            "fooled in general.",
        ]
    out.append("")

    # Licences
    out += [
        "## Licences and credit",
        "",
        f"This dataset is released under "
        f"**{LICENCE_NAMES.get(cfg.dataset.license, cfg.dataset.license)}** "
        "(share-alike, because it contains CC BY-SA 4.0 material). The repository is private.",
        "",
        "- **BIPIA** (Microsoft): MIT. Its table and code contexts are CC BY-SA 4.0 (share-alike).",
        "- **Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1** (NVIDIA): CC BY 4.0, "
        "<https://creativecommons.org/licenses/by/4.0/>. Credit: NVIDIA. Changes: see *How "
        "`nemotron` was built*.",
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


def stage(cfg: Config, version: str, card: str, out_dir: Path) -> list[Path]:
    """Copy the parquet files this version adds, and write README.md, into a fresh staging
    folder. v1 stages only `bipia_textfooler` (+ the card): v0 files are never uploaded again."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    files: list[Path] = []
    for config in new_configs(version):
        for split in SPLITS[config]:
            dst = out_dir / config / f"{split}.parquet"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(parquet_path(cfg, config, split), dst)
            files.append(dst)
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
            if tuple(got.get("columns", ())) != COLUMNS:
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
    files = stage(cfg, version, card, out_dir)
    hits = scan_secrets(files)
    report_path(cfg, card_copy_name(version)).write_text(card, encoding="utf-8", newline="\n")
    gate = p7_gate(cfg, version, configs)
    payload: dict[str, Any] = {
        "action": "push" if do_push else "dry-run",
        "version": version,
        "configs": configs,
        "uploads": new_configs(version),
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
