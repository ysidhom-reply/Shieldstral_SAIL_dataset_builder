"""Read-only access to nvidia/Nemotron-RL-Agentic-Indirect-Prompt-Injection-v1 (CC BY 4.0).

The dataset is public: it is downloaded without a token (HF_TOKEN is only needed in P10).
Only the JSONL data file is read; nothing from the repository is executed.
"""

import json
import logging
from pathlib import Path
from typing import Any

from dataset_builder.config import Config

logger = logging.getLogger(__name__)

HF_SPLIT = "train"  # the only split released


class NemotronSourceError(RuntimeError):
    pass


def raw_dir(cfg: Config) -> Path:
    return cfg.paths.raw / "nemotron"


def fetch_nemotron(cfg: Config) -> tuple[Path, str]:
    """Download the data file at the pinned revision (or reuse it). Returns (path, revision)."""
    n = cfg.nemotron
    path = raw_dir(cfg) / n.filename
    if path.is_file() and n.revision:
        return path, n.revision
    from huggingface_hub import HfApi, hf_hub_download

    revision = n.revision or HfApi().dataset_info(n.repo_id, token=False).sha
    if not n.revision:
        logger.warning("No revision pinned: using %s. Pin it in configs/config.yaml.", revision)
    hf_hub_download(
        n.repo_id,
        n.filename,
        repo_type="dataset",
        revision=revision,
        local_dir=raw_dir(cfg),
        token=False,
    )
    return path, revision


def user_request(record: dict[str, Any]) -> str:
    msgs = [m for m in record["responses_create_params"]["input"] if m.get("role") == "user"]
    if len(msgs) != 1:
        raise NemotronSourceError(f"record {record['id']}: {len(msgs)} user messages, expected 1")
    return msgs[0]["content"]


def load_records(path: Path) -> list[dict[str, Any]]:
    """The fields the brief names, one dict per record, in file order."""
    out = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            inj = r["injection"]
            out.append(
                {
                    "id": r["id"],
                    "domain": r["domain"],
                    "attack_category": r["attack_category"],
                    "injection_vector": r["injection_vector"],
                    "injection_text": inj["injection_text"],
                    "goal": inj["goal"],
                    "target_tool": inj["target_tool"],
                    "target_args": inj["target_args"],
                    "environment": r["environment"],
                    "user_request": user_request(r),
                }
            )
    return out
