"""Read-only access to the BIPIA repository (github.com/microsoft/BIPIA).

Only data files are read (jsonl / json). No code from the repository is imported or executed.
"""

import json
import logging
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from dataset_builder.config import Config

logger = logging.getLogger(__name__)

ATTACK_FILES: dict[tuple[str, str], str] = {
    ("text", "train"): "text_attack_train.json",
    ("text", "test"): "text_attack_test.json",
    ("code", "train"): "code_attack_train.json",
    ("code", "test"): "code_attack_test.json",
}
OFFICIAL_SPLITS = ("train", "test")


class BipiaSourceError(RuntimeError):
    pass


def bipia_root(cfg: Config) -> Path:
    return cfg.paths.raw / "BIPIA"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def fetch_bipia(cfg: Config) -> str:
    """Clone BIPIA into data/raw/BIPIA if missing. Returns the HEAD commit SHA.

    If `bipia.repo_commit` is set, the checked-out commit must match it.
    """
    root = bipia_root(cfg)
    if not (root / ".git").exists():
        logger.info("Cloning %s into %s", cfg.bipia.repo_url, root)
        root.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--quiet", cfg.bipia.repo_url, str(root))
    sha = _git("rev-parse", "HEAD", cwd=root)
    if cfg.bipia.repo_commit and sha != cfg.bipia.repo_commit:
        raise BipiaSourceError(
            f"BIPIA checkout is at {sha}, but config pins repo_commit={cfg.bipia.repo_commit}."
        )
    return sha


def context_path(root: Path, scenario: str, split: str) -> Path:
    return root / "benchmark" / scenario / f"{split}.jsonl"


def load_contexts(root: Path, scenario: str, split: str) -> list[dict[str, Any]]:
    """Raw rows of benchmark/<scenario>/<split>.jsonl, each with its 1-based `_row` number."""
    rows: list[dict[str, Any]] = []
    path = context_path(root, scenario, split)
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if line.strip():
                rows.append(json.loads(line) | {"_row": i})
    return rows


def load_attacks(root: Path) -> dict[tuple[str, str], dict[str, list[str]]]:
    """The four attack files: {(group, source_split): {category: [5 strings]}}, file order kept."""
    out: dict[tuple[str, str], dict[str, list[str]]] = {}
    for key, name in ATTACK_FILES.items():
        data = json.loads((root / "benchmark" / name).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(v, list) for v in data.values()):
            raise BipiaSourceError(f"{name}: expected an object of category -> list of strings")
        out[key] = data
    return out


def _type_name(v: Any) -> str:
    if isinstance(v, list):
        inner = sorted({type(x).__name__ for x in v}) or ["empty"]
        return f"list[{'|'.join(inner)}]"
    return type(v).__name__


def _example(v: Any, width: int = 80) -> str:
    s = json.dumps(v, ensure_ascii=False)
    s = s if len(s) <= width else s[: width - 1] + "…"
    return s.replace("|", "\\|")


def describe_formats(root: Path, scenarios: list[str]) -> str:
    """Markdown description of every file read: rows, fields, types and a short example."""
    lines = ["### Context files", ""]
    for scenario in scenarios:
        for split in OFFICIAL_SPLITS:
            rows = load_contexts(root, scenario, split)
            types: dict[str, Counter[str]] = {}
            for r in rows:
                for k, v in r.items():
                    if k != "_row":
                        types.setdefault(k, Counter())[_type_name(v)] += 1
            rel = context_path(root, scenario, split).relative_to(root).as_posix()
            lines += [
                f"**`{rel}`**: {len(rows)} rows",
                "",
                "| field | type | example |",
                "|---|---|---|",
            ]
            for k, c in types.items():
                t = ", ".join(f"{n} ({m})" if len(c) > 1 else n for n, m in c.most_common())
                lines.append(f"| `{k}` | {t} | {_example(rows[0].get(k))} |")
            lines.append("")
    lines += [
        "### Attack files",
        "",
        "| file | top-level | categories | strings | strings per category |",
    ]
    lines.append("|---|---|---:|---:|---|")
    for (group, split), data in load_attacks(root).items():
        per = sorted({len(v) for v in data.values()})
        lines.append(
            f"| `benchmark/{ATTACK_FILES[(group, split)]}` | object: category -> list[str] | "
            f"{len(data)} | {sum(map(len, data.values()))} | {per} |"
        )
    return "\n".join(lines) + "\n"
