"""Project configuration, loaded from `configs/config.yaml`.

Secrets are never part of the config: use `require_secret`, which reads the environment or a
git-ignored `.env` file at the repository root.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "config.yaml"

SecretName = Literal["HF_TOKEN", "MISTRAL_API_KEY"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasetCfg(_Strict):
    repo_id: str
    private: bool = True
    configs: list[str]


class PathsCfg(_Strict):
    raw: Path
    interim: Path
    out: Path
    reports: Path


class TextsCfg(_Strict):
    instruct: str
    query_template: str


class BipiaCfg(_Strict):
    repo_url: str
    repo_commit: str | None = None
    scenarios: list[Literal["email", "table", "code"]]
    tables_moved_to_test: int
    validation_size: dict[str, int]
    expected_cross_split_emails: int
    allowed_shared_attack_groups: list[str] = []
    split: Literal["three_way"]
    test_positions_per_pair: int
    n_per_doc: int
    positions: list[Literal["start", "middle", "end"]]


class BenignCheckCfg(_Strict):
    judge_model: str
    shieldstral_4bit: Literal["auto", "on", "off"]


class TextFoolerCfg(_Strict):
    victim_model: str


class NemotronCfg(_Strict):
    repo_id: str
    filename: str = "train.jsonl"
    revision: str | None = None
    goal_cap: int
    hard_negative_words: list[str]
    sentence_repairs: Path


class P3GeneratorCfg(_Strict):
    model: str
    reasoning_effort: str | None = "none"
    temperature: tuple[float, float]
    max_tokens: dict[str, int]  # per scenario group: text, code


class ShortPoolCfg(_Strict):
    """A pool allowed to end below `n` texts (team-approved, documented in the P3 report)."""

    doc_id: str
    position: Literal["start", "middle", "end"]
    min_texts: int  # at least this many, and at least what P4 needs for this pool
    reason: str
    approved: str  # who approved it and when


class P3Cfg(_Strict):
    prompts: Path
    generator: P3GeneratorCfg
    n: int
    mix: dict[Literal["short", "medium", "long"], int]
    mix_cap: int | None = 10  # max texts of one size per pool (None = no cap)
    regen_extra: int = 4  # extra texts asked for in a regeneration round
    top_up_from_spares: bool = True  # after the last round, fill short pools from set-aside texts
    accepted_short_pools: list[ShortPoolCfg] = []
    judge_batch_size: dict[str, int] = {}  # texts per judge call, per scenario (default 1)
    judge_control: int = 40  # positive control: BIPIA attacks sent to the judge
    judge_control_min_rate: float = 0.8
    lengths: dict[Literal["text", "code"], list[int]]
    length_range: tuple[float, float]  # x len_short .. x len_long
    context_chars: int
    max_doc_chars: int
    max_rounds: int
    concurrency: int
    max_retries: int
    near_dup_ratio: float
    attack_overlap_words: int
    handread_per_group: int
    assistant_words: list[str]
    assistant_words_case_sensitive: list[str]
    assistant_words_text_only: list[str]
    code_intro_patterns: list[str]
    code_forbidden_patterns: list[str]
    prices: dict[str, dict[str, float]]  # model prefix -> {input, output} $ per 1M tokens


class Config(_Strict):
    seed: int
    dataset: DatasetCfg
    paths: PathsCfg
    pilot_size: int
    texts: TextsCfg
    bipia: BipiaCfg
    benign_check: BenignCheckCfg
    textfooler: TextFoolerCfg
    nemotron: NemotronCfg
    p3: P3Cfg
    expected_counts: dict[str, dict[str, Any]] = {}


@lru_cache
def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    """Load the YAML config. Relative paths are resolved against the repository root."""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = Config.model_validate(raw)
    root = path.resolve().parent.parent
    resolved = {k: (v if v.is_absolute() else root / v) for k, v in cfg.paths.model_dump().items()}
    rep = cfg.nemotron.sentence_repairs
    nem = cfg.nemotron.model_copy(
        update={"sentence_repairs": rep if rep.is_absolute() else root / rep}
    )
    pr = cfg.p3.prompts
    p3 = cfg.p3.model_copy(update={"prompts": pr if pr.is_absolute() else root / pr})
    return cfg.model_copy(update={"paths": PathsCfg(**resolved), "nemotron": nem, "p3": p3})


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser: blank lines and # comments ignored, optional quotes stripped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        values[key] = val
    return values


def require_secret(name: SecretName, dotenv: Path | None = None) -> str:
    """Return a secret: the environment variable first, else the git-ignored `.env` file.

    The value is only returned: it is never logged, copied into os.environ, cached or written.
    """
    value = os.environ.get(name) or _read_dotenv(dotenv or REPO_ROOT / ".env").get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Put it in the git-ignored .env file at the repository root "
            f"({name}=...) or set the environment variable."
        )
    return value
