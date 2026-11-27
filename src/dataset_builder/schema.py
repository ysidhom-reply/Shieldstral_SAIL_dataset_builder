"""Shared sample schema: the same columns, in the same order, for every dataset config."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from dataset_builder.config import Config, load_config

Source = Literal["bipia", "nemotron", "bipia_textfooler"]
Split = Literal["train", "validation", "test"]
BipiaScenario = Literal["email", "table", "code"]
LabelProvenance = Literal["construction", "injection_metadata", "validated"]
GenerationMethod = Literal[
    "sampled_insertion",
    "llm_benign_insertion",
    "original",
    "counterfactual",
    "textfooler",
    "control",
    "llm_textfooler_style",
]
Position = Literal["start", "middle", "end"]

BIPIA_SCENARIOS: frozenset[str] = frozenset(BipiaScenario.__args__)

# Fields allowed only for some sources (BIPIA fields carry over to TextFooler rows).
BIPIA_FIELDS = (
    "attack_group",
    "attack_name",
    "position",
    "insert_index",
    "benign_id",
    "benign_span",
    "label_checked",
)
NEMOTRON_FIELDS = (
    "document_path",
    "injection_vector",
    "attack_category",
    "goal_group",
    "wrapper",
    "wrapper_outside_span",
    "sentence_repaired",
)
TEXTFOOLER_FIELDS = ("victim_model",)
# Extra columns of the `bipia_textfooler` config only (v0 files keep their 30 columns). The test
# variants (P6) and the training variants (P8) have different judges, so the judge and the
# generator are per row; settings that are the same on every row go in the dataset card, and
# pipeline bookkeeping (judge verdicts, attempt status) stays in data/interim and reports.
TEXTFOOLER_ONLY_FIELDS = ("p_yes_base", "generator_model", "judge_model", "nested_size")
NESTED_SIZES = (500, 1000, 2000, 4000)  # the brief's sizes (the config sets the ones used)

# Allowed generation methods per (source, label).
_METHODS: dict[tuple[str, int], set[str]] = {
    ("bipia", 1): {"sampled_insertion"},
    ("bipia", 0): {"llm_benign_insertion"},
    ("nemotron", 1): {"original"},
    ("nemotron", 0): {"counterfactual"},
    ("bipia_textfooler", 1): {"textfooler", "llm_textfooler_style"},
    ("bipia_textfooler", 0): {"textfooler", "control", "llm_textfooler_style"},
}


def instruct_text(cfg: Config | None = None) -> str:
    """The fixed policy text, identical for every sample."""
    return (cfg or load_config()).texts.instruct


def build_query(user_request: str, cfg: Config | None = None) -> str:
    """Fill the fixed query template with the user's request."""
    return (cfg or load_config()).texts.query_template.format(user_request=user_request)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str
    parent_id: str | None = None
    source: Source
    split: Split
    source_split: str
    scenario: str
    instruct: str
    query: str
    document: str
    label: Literal[0, 1]
    answer: Literal["yes", "no"]
    label_provenance: LabelProvenance
    generation_method: GenerationMethod
    attack_group: str | None = None
    attack_name: str | None = None
    position: Position | None = None
    insert_index: int | None = None
    injection_span: str | None = None
    protected_tokens: list[str] | None = None
    benign_id: str | None = None
    benign_span: list[int] | None = None  # [start, end) of the inserted benign text in document
    label_checked: bool | None = None
    document_path: str | None = None
    injection_vector: str | None = None
    attack_category: str | None = None
    goal_group: str | None = None
    wrapper: str | None = None
    wrapper_outside_span: bool | None = None
    sentence_repaired: bool | None = None
    victim_model: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        errors: list[str] = []
        if self.answer != ("yes" if self.label == 1 else "no"):
            errors.append(f"answer {self.answer!r} does not match label {self.label}")
        if self.instruct != instruct_text():
            errors.append("instruct differs from the fixed policy text")
        if self.source != "nemotron" and self.scenario not in BIPIA_SCENARIOS:
            errors.append(f"scenario {self.scenario!r} is not a BIPIA scenario")
        if self.generation_method not in _METHODS[(self.source, self.label)]:
            errors.append(
                f"generation_method {self.generation_method!r} invalid for "
                f"source={self.source}, label={self.label}"
            )
        allowed = {
            "bipia": BIPIA_FIELDS,
            "nemotron": NEMOTRON_FIELDS,
            "bipia_textfooler": BIPIA_FIELDS + TEXTFOOLER_FIELDS,
        }[self.source]
        for f in BIPIA_FIELDS + NEMOTRON_FIELDS + TEXTFOOLER_FIELDS:
            if f not in allowed and getattr(self, f) is not None:
                errors.append(f"{f} must be None for source={self.source}")
        for f in TEXTFOOLER_ONLY_FIELDS:
            if self.source != "bipia_textfooler" and getattr(self, f, None) is not None:
                errors.append(f"{f} must be None for source={self.source}")
        if self.source == "bipia" and self.label == 1:
            for f in ("benign_id", "benign_span", "label_checked"):
                if getattr(self, f) is not None:
                    errors.append(f"{f} is only set on BIPIA negatives")
        if self.benign_span is not None:
            sp = self.benign_span
            if len(sp) != 2 or not 0 <= sp[0] < sp[1] <= len(self.document):
                errors.append(f"benign_span {sp} is not [start, end) inside the document")
        if errors:
            raise ValueError("; ".join(errors))
        return self


class TextFoolerSample(Sample):
    """A `bipia_textfooler` row: the shared columns plus the TextFooler-only ones.

    Test rows (P6): `textfooler` variants and `control` twins, split test, no generator.
    Training rows (P8): `llm_textfooler_style`, split train or validation, written by
    `generator_model`, judged by `judge_model`, no attacked model (`victim_model` empty);
    `nested_size` (train only) is the smallest nested training subset that holds the row."""

    p_yes_base: float | None = None  # base Shieldstral's probability of yes on this row
    generator_model: str | None = None  # P8: the LLM that wrote the variant
    judge_model: str | None = None  # the model that judged the variant (None on a control)
    nested_size: int | None = None  # P8 train: smallest nested subset holding it (None = none)

    @model_validator(mode="after")
    def _check_textfooler(self) -> Self:
        errors: list[str] = []
        llm = self.generation_method == "llm_textfooler_style"
        if llm:
            if self.split not in ("train", "validation"):
                errors.append("a training variant must be in train or validation")
            if not self.generator_model or not self.judge_model:
                errors.append("a training variant needs generator_model and judge_model")
            if self.victim_model is not None:
                errors.append("a training variant was not attacked: victim_model must be None")
            if self.nested_size is not None and (self.split != "train" or self.nested_size <= 0):
                errors.append(f"nested_size {self.nested_size} invalid for split {self.split}")
        else:
            if self.split != "test":
                errors.append("a TextFooler test variant or control must be in test")
            if self.generator_model is not None or self.nested_size is not None:
                errors.append("generator_model and nested_size are only set on training variants")
        if errors:
            raise ValueError("; ".join(errors))
        return self


COLUMNS: tuple[str, ...] = tuple(Sample.model_fields)
COLUMNS_TEXTFOOLER: tuple[str, ...] = tuple(TextFoolerSample.model_fields)


def columns_for(config: str) -> tuple[str, ...]:
    """Expected parquet columns of a dataset config."""
    return COLUMNS_TEXTFOOLER if config == "bipia_textfooler" else COLUMNS
