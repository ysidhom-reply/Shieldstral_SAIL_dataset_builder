"""P6 victim: base Shieldstral's probability of "yes" on a sample.

    p(yes) = exp(z_yes) / (exp(z_yes) + exp(z_no))

from the two answer-token logits at the last position, for the fixed instruct, the sample's query
and the WHOLE document (no windows), through Shieldstral's own chat template.

The model code (torch, transformers) is imported lazily: this file imports and is tested on a
machine without a GPU. `Scorer` is the interface the attack and the twins use; tests give it a
fake.
"""

import logging
import math
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from dataset_builder.config import TextFoolerCfg

logger = logging.getLogger(__name__)

# (instruct, query, document)
Item = tuple[str, str, str]


def p_yes_from_logits(z_yes: float, z_no: float) -> float:
    """Softmax over the two answer tokens, stable for large logits."""
    m = max(z_yes, z_no)
    ey, en = math.exp(z_yes - m), math.exp(z_no - m)
    return ey / (ey + en)


def build_messages(instruct: str, query: str, document: str) -> list[dict[str, str]]:
    """The chat messages for one sample. The policy is the system message; the user message is
    the untrusted document followed by the query. CHECK on the GPU machine against Shieldstral's
    model card (this layout is the one place the prompt is decided)."""
    return [
        {"role": "system", "content": instruct},
        {"role": "user", "content": f"<document>\n{document}\n</document>\n\n{query}"},
    ]


def resolve_answer_ids(
    encode: Callable[[str], list[int]], answers: dict[str, str]
) -> dict[str, int]:
    """The token id of each answer word. Each must be exactly one token: otherwise the logit of
    "the answer" is not defined, and the run stops."""
    ids: dict[str, int] = {}
    for key in ("yes", "no"):
        toks = encode(answers[key])
        if len(toks) != 1:
            raise ValueError(
                f"answer token {answers[key]!r} is {len(toks)} tokens ({toks}), expected 1: "
                "change `answer_tokens` in the config"
            )
        ids[key] = toks[0]
    if ids["yes"] == ids["no"]:
        raise ValueError("the yes and no answer tokens are the same token")
    return ids


class Scorer(Protocol):
    """Scores samples. `score` returns p(yes) per item; `info` is written to the report."""

    def score(self, items: Sequence[Item]) -> list[float]: ...

    def info(self) -> dict[str, Any]: ...


class CachedScorer:
    """Wraps a Scorer with a cache on the exact (instruct, query, document): TextFooler asks for
    the same text many times. Counts what it was asked and what the model really scored."""

    def __init__(self, inner: Scorer, max_entries: int = 50_000) -> None:
        self.inner, self.max_entries = inner, max_entries
        self.cache: dict[Item, float] = {}
        self.requested = 0
        self.scored = 0

    def score(self, items: Sequence[Item]) -> list[float]:
        self.requested += len(items)
        todo = list(dict.fromkeys(i for i in items if i not in self.cache))
        if todo:
            for item, p in zip(todo, self.inner.score(todo), strict=True):
                self.cache[item] = p
            self.scored += len(todo)
        out = [self.cache[i] for i in items]
        if len(self.cache) > self.max_entries:
            self.cache.clear()
        return out

    def info(self) -> dict[str, Any]:
        return self.inner.info() | {
            "documents_requested": self.requested,
            "documents_scored": self.scored,
        }


class ShieldstralScorer:
    """Base Shieldstral on the GPU. Needs torch and transformers (not imported until built)."""

    def __init__(self, tf: TextFoolerCfg) -> None:
        try:
            import torch
            from huggingface_hub import HfApi
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as e:  # pragma: no cover - environment message
            raise RuntimeError(f"P6 needs torch and transformers on the GPU machine ({e})") from e
        if not torch.cuda.is_available():
            raise RuntimeError("P6 scores with base Shieldstral and needs a CUDA GPU")
        self.torch = torch
        self.tf = tf
        revision = tf.victim_revision
        if not revision:
            revision = HfApi().model_info(tf.victim_model).sha
            logger.warning("victim revision not pinned: using %s (pin it in the config)", revision)
        self.revision = revision
        self.tok = AutoTokenizer.from_pretrained(tf.victim_model, revision=revision)
        self.tok.padding_side = "left"
        if self.tok.pad_token_id is None:
            self.tok.pad_token = self.tok.eos_token
        dtype = getattr(torch, tf.victim_dtype)
        try:
            self.model = AutoModelForCausalLM.from_pretrained(
                tf.victim_model, revision=revision, torch_dtype=dtype, device_map="cuda"
            )
        except (ValueError, KeyError):  # a multimodal checkpoint: load its text-capable class
            from transformers import AutoModelForImageTextToText

            self.model = AutoModelForImageTextToText.from_pretrained(
                tf.victim_model, revision=revision, torch_dtype=dtype, device_map="cuda"
            )
        self.model.eval()
        self.ids = resolve_answer_ids(
            lambda w: self.tok.encode(w, add_special_tokens=False), tf.answer_tokens
        )

    def prompt(self, item: Item) -> str:
        return self.tok.apply_chat_template(
            build_messages(*item), tokenize=False, add_generation_prompt=True
        )

    def score(self, items: Sequence[Item]) -> list[float]:
        torch = self.torch
        order = sorted(range(len(items)), key=lambda i: len(items[i][2]))  # similar lengths
        out = [0.0] * len(items)
        bs = self.tf.score_batch_size
        for start in range(0, len(order), bs):
            idx = order[start : start + bs]
            enc = self.tok(
                [self.prompt(items[i]) for i in idx],
                return_tensors="pt",
                padding=True,
                add_special_tokens=False,  # the chat template already added them
            ).to(self.model.device)
            with torch.no_grad():
                logits = self.model(**enc).logits[:, -1, :].float()
            zy, zn = logits[:, self.ids["yes"]], logits[:, self.ids["no"]]
            p = torch.softmax(torch.stack([zy, zn], dim=-1), dim=-1)[:, 0]
            for i, v in zip(idx, p.tolist(), strict=True):
                out[i] = v
        return out

    def info(self) -> dict[str, Any]:
        import importlib.metadata as md

        return {
            "victim": f"{self.tf.victim_model}@{self.revision}",
            "victim_dtype": self.tf.victim_dtype,
            "answer_token_ids": self.ids,
            "versions": {
                k: md.version(k) for k in ("torch", "transformers", "textattack") if _has(k)
            },
        }


def _has(pkg: str) -> bool:
    import importlib.metadata as md

    try:
        md.version(pkg)
    except md.PackageNotFoundError:
        return False
    return True
