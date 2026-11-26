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
    pretty_name: str = "ipi-twins"
    license: str = "cc-by-sa-4.0"


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
    victim_revision: str | None = None  # pinned Hugging Face commit
    victim_path: Path | None = None  # local weights at that revision (None = download)
    victim_dtype: Literal["bfloat16", "float16", "float32"] = "bfloat16"
    answer_tokens: dict[Literal["yes", "no"], str] = {"yes": "yes", "no": "no"}
    score_batch_size: int = 8  # documents per victim forward pass
    threshold: float = 0.5  # malicious succeeds below it; a benign twin flips at or above it
    # TextFooler recipe values (Jin et al. 2019, TextAttack); checked against the recipe at run.
    max_candidates: int = 50
    min_cos_sim: float = 0.5
    use_threshold: float = 0.840845057
    use_window: int = 15
    max_span_fraction: float = 0.30  # at most 30% of the span's words changed
    judge_model: str
    judge_revision: str | None = None
    judge_path: Path | None = None  # local weights (pinned download)
    judge_backend: Literal["transformers", "server"] = "transformers"  # in-process or local server
    judge_batch_size: int = 8  # prompts per generate call (transformers backend)
    judge_keep_loaded: bool = False  # False: free the GPU after each judge phase
    judge_base_url: str = "http://127.0.0.1:11434/v1"  # must be this machine
    judge_reasoning_effort: Literal["low", "medium", "high"] = "low"
    judge_max_tokens: int = 1024
    judge_timeout_s: float = 300
    round_size: int = 300  # candidates per round: victim phase, then judge phase
    chunk_size: int = 50  # the victim phase saves its results every this many candidates
    pilot_pairs: int = 200
    max_per_doc: int = 3  # attempted pairs per document
    targets: dict[str, int] = {}  # kept malicious variants per scenario
    floor_per_scenario: int = 300
    max_per_scenario: int = 500
    attempt_cap: dict[str, int | None] = {}  # pairs attempted per scenario (None = no cap)
    translation_slice: int = 100  # kept variants of the translation group (0 = no slice)
    translation_group: str = "Language Translation"
    twin_sets: int = 1000
    human_check_n: int = 80
    max_projected_hours: float = 72


class GeneratorCfg(_Strict):
    """P8: the local LLM that writes training variants."""

    model: str
    revision: str | None = None
    path: Path | None = None


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


class P3ExtensionCfg(_Strict):
    """Extra texts for some pools after the normal rounds (see extend_pools)."""

    target_n: int = 20  # 20 = no extension
    splits: list[str] = []
    scenarios: list[str] = []
    mix: dict[Literal["short", "medium", "long"], int] = {"short": 7, "medium": 9, "long": 9}
    max_rounds: int = 8


class P3HardNegCfg(_Strict):
    """Option B (team decision, 2026-11-24): replace part of every pool with harmless texts that
    sound like instructions to the human reader, so instruction wording stops being a shortcut."""

    share: float = 0.4  # share of each pool replaced (8 of 20, 10 of 25)
    prompt_version: str = "hn-v1"
    judge_prompt_version: str = "judge-v4"
    judge_batch_prompt_version: str = "judge-batch-v2"
    max_rounds: int = 8
    # Texts with the most of these words are replaced first (P7 v0 diagnostic 4).
    replace_words: list[str] = []
    # Filter profile for the new texts (the existing texts keep the brief's filters).
    assistant_words: list[str] = []
    assistant_words_text_only: list[str] = []
    code_forbidden_patterns: list[str] = []
    allowed_url_hosts: list[str] = []


class P3Cfg(_Strict):
    prompts: Path
    generator: P3GeneratorCfg
    n: int
    mix: dict[Literal["short", "medium", "long"], int]
    mix_cap: int | None = 10  # max texts of one size per pool (None = no cap)
    regen_extra: int = 4  # extra texts asked for in a regeneration round
    top_up_from_spares: bool = True  # after the last round, fill short pools from set-aside texts
    accepted_short_pools: list[ShortPoolCfg] = []
    extension: P3ExtensionCfg = P3ExtensionCfg()
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
    allowed_url_hosts: list[str] = []  # URLs on these hosts pass the URL filter (hard negatives)
    hard_negatives: P3HardNegCfg = P3HardNegCfg()


class P4Cfg(_Strict):
    # A pool text is "length-matched" if |len(text) - len(attack)| <= window x len(attack).
    length_window: dict[Literal["text", "code"], float]
    # Option (a) after P7 v0 (team decision, 2026-11-24): among the least-used texts within the
    # length window, prefer the one with the fewest of these over-represented benign words.
    avoid_words: list[str] = []


class P7TfidfCfg(_Strict):
    ngram: tuple[int, int] = (1, 2)
    min_df: int = 5
    max_features: int = 50_000
    C: float = 1.0


class P7Cfg(_Strict):
    # Stop rule: the team's limits. A baseline above them blocks the push; P7 never edits them.
    length_auc_stop: float = 0.70
    tfidf_auc_stop: float = 0.95
    fpr: float = 0.01  # recall is reported at this false-positive rate
    # Position shares: allowed distance from one third, per split.
    position_tolerance: dict[str, float] = {"test": 0.02, "train": 0.05, "validation": 0.05}
    tfidf: P7TfidfCfg = P7TfidfCfg()


class Config(_Strict):
    seed: int
    dataset: DatasetCfg
    paths: PathsCfg
    pilot_size: int
    texts: TextsCfg
    bipia: BipiaCfg
    benign_check: BenignCheckCfg
    textfooler: TextFoolerCfg
    generator: GeneratorCfg | None = None
    nemotron: NemotronCfg
    p3: P3Cfg
    p4: P4Cfg
    p7: P7Cfg = P7Cfg()
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

    def under_root(p: Path | None) -> Path | None:
        return p if p is None or p.is_absolute() else root / p

    tf = cfg.textfooler.model_copy(
        update={
            "victim_path": under_root(cfg.textfooler.victim_path),
            "judge_path": under_root(cfg.textfooler.judge_path),
        }
    )
    update: dict[str, Any] = {
        "paths": PathsCfg(**resolved),
        "nemotron": nem,
        "p3": p3,
        "textfooler": tf,
    }
    if cfg.generator is not None:
        update["generator"] = cfg.generator.model_copy(
            update={"path": under_root(cfg.generator.path)}
        )
    return cfg.model_copy(update=update)


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
