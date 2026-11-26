"""P6 victim: base Shieldstral's probability of "yes" on a sample.

    p(yes) = exp(z_yes) / (exp(z_yes) + exp(z_no))

at the last position, for the sample's instruct, query and the WHOLE document (no windows). The
prompt and the score follow the reference code of the model card (mistralai/Shieldstral-1.0-3B):
a fixed system message, a user message `<Instruct>: ...\n\n<Query>: ...\n\n<Document>: ...`, the
mistral-common tokenizer, and z_yes / z_no the highest logit among the tokens that read "yes" /
"no" (stripped, lower-case: yes, yes., "yes", 'yes').

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


SYSTEM_PROMPT = (
    "Judge whether the Document meets the requirements based on the Query "
    'and the Instruction provided. Note that the answer can only be "yes" or "no".'
)


def build_messages(instruct: str, query: str, document: str) -> list[dict[str, str]]:
    """The chat messages for one sample, as in the model card."""
    user = f"<Instruct>: {instruct}\n\n<Query>: {query}\n\n<Document>: {document}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def answer_variants(word: str) -> set[str]:
    w = word.strip().lower()
    return {w, f"{w}.", f'"{w}"', f"'{w}'"}


def resolve_answer_ids(
    decode: Callable[[int], str], vocab_size: int, answers: dict[str, str]
) -> dict[str, list[int]]:
    """Every token id that reads as each answer (stripped, lower-case), as in the model card's
    reference code. The run stops if an answer has no token, or a token reads as both."""
    want = {k: answer_variants(answers[k]) for k in ("yes", "no")}
    if want["yes"] & want["no"]:
        raise ValueError("the yes and no answer words are the same")
    ids: dict[str, list[int]] = {"yes": [], "no": []}
    for i in range(vocab_size):
        t = decode(i).strip().lower()
        for k in ids:
            if t in want[k]:
                ids[k].append(i)
    for k in ids:
        if not ids[k]:
            raise ValueError(
                f"no token reads as {answers[k]!r}: change `answer_tokens` in the config"
            )
    return ids


def left_pad(seqs: list[list[int]], pad_id: int) -> tuple[list[list[int]], list[list[int]]]:
    """Token ids padded on the left (the answer slot stays last) and the attention mask."""
    n = max(len(x) for x in seqs)
    ids = [[pad_id] * (n - len(x)) + x for x in seqs]
    mask = [[0] * (n - len(x)) + [1] * len(x) for x in seqs]
    return ids, mask


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

    def release(self) -> None:
        """Free the model's GPU memory; the cache (plain numbers) is kept."""
        fn = getattr(self.inner, "release", None)
        if callable(fn):
            fn()

    def info(self) -> dict[str, Any]:
        return self.inner.info() | {
            "documents_requested": self.requested,
            "documents_scored": self.scored,
        }


class ShieldstralScorer:
    """Base Shieldstral on the GPU. Needs torch and transformers (not imported until built). The
    tokenizer is loaded at once; the model on the first `score` and again after `release`."""

    def __init__(self, tf: TextFoolerCfg) -> None:
        try:
            import torch
            import transformers
            from huggingface_hub import HfApi
        except ImportError as e:  # pragma: no cover - environment message
            raise RuntimeError(f"P6 needs torch and transformers on the GPU machine ({e})") from e
        if not torch.cuda.is_available():
            raise RuntimeError("P6 scores with base Shieldstral and needs a CUDA GPU")
        self.torch = torch
        self.tf = tf
        revision = tf.victim_revision
        if tf.victim_path is not None:  # local weights, downloaded at the pinned revision
            if not (tf.victim_path / "config.json").is_file():
                raise RuntimeError(f"victim_path {tf.victim_path} has no model: download it first")
            if not revision:
                raise RuntimeError("victim_path is set: pin victim_revision to the downloaded one")
            source, load_rev = str(tf.victim_path), None
        else:
            if not revision:
                revision = HfApi().model_info(tf.victim_model).sha
                logger.warning("victim revision not pinned: using %s (pin it)", revision)
            source, load_rev = tf.victim_model, revision
        self.revision = revision
        self.source, self.load_rev = source, load_rev
        backend = getattr(transformers, "MistralCommonBackend", None)
        try:  # the model card's tokenizer (needs mistral-common)
            if backend is None:
                raise ImportError("transformers has no MistralCommonBackend")
            self.tok = backend.from_pretrained(source, revision=load_rev)
            self.tokenizer_kind = "mistral-common"
        except ImportError as e:
            logger.warning("mistral-common tokenizer unavailable (%s): using AutoTokenizer", e)
            self.tok = transformers.AutoTokenizer.from_pretrained(source, revision=load_rev)
            self.tokenizer_kind = "auto"
        pad = getattr(self.tok, "pad_token_id", None)
        self.pad_id = pad if pad is not None else 0
        self.model: Any = None
        self.loads = 0
        self.peak_gb = 0.0
        vocab = len(self.tok) if hasattr(self.tok, "__len__") else self.tok.vocab_size
        self.ids = resolve_answer_ids(lambda i: self.tok.decode([i]), vocab, tf.answer_tokens)

    def _load(self) -> None:
        from transformers import AutoModelForImageTextToText

        dtype = getattr(self.torch, self.tf.victim_dtype)
        # Mistral3ForConditionalGeneration (text + vision); only text is used here
        self.model = AutoModelForImageTextToText.from_pretrained(
            self.source, revision=self.load_rev, dtype=dtype, device_map="cuda"
        )
        self.model.eval()
        self.loads += 1

    def release(self) -> None:
        """Free the GPU for the judge; the next `score` loads the model again."""
        if self.model is None:
            return
        import gc

        self.peak_gb = max(self.peak_gb, self.torch.cuda.max_memory_allocated() / 2**30)
        self.model = None
        gc.collect()
        self.torch.cuda.empty_cache()

    def encode(self, item: Item) -> list[int]:
        """Token ids of one sample through the chat template (special tokens included)."""
        out = self.tok.apply_chat_template(build_messages(*item), tokenize=True)
        if isinstance(out, dict) or hasattr(out, "keys"):
            out = out["input_ids"]
        return list(out)

    def score(self, items: Sequence[Item]) -> list[float]:
        torch = self.torch
        if self.model is None:
            self._load()
        order = sorted(range(len(items)), key=lambda i: len(items[i][2]))  # similar lengths
        out = [0.0] * len(items)
        bs = self.tf.score_batch_size
        for start in range(0, len(order), bs):
            idx = order[start : start + bs]
            ids, mask = left_pad([self.encode(items[i]) for i in idx], self.pad_id)
            dev = self.model.device
            with torch.no_grad():
                logits = (
                    self.model(
                        input_ids=torch.tensor(ids, device=dev),
                        attention_mask=torch.tensor(mask, device=dev),
                        logits_to_keep=1,
                    )
                    .logits[:, -1, :]
                    .float()
                )
            zy = logits[:, self.ids["yes"]].max(dim=-1).values
            zn = logits[:, self.ids["no"]].max(dim=-1).values
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
            "tokenizer": self.tokenizer_kind,
            "victim_loads": self.loads,
            "victim_peak_gpu_gb": round(self.peak_gb, 2),
            "versions": {
                k: md.version(k)
                for k in ("torch", "transformers", "mistral-common", "textattack")
                if _has(k)
            },
        }


def _has(pkg: str) -> bool:
    import importlib.metadata as md

    try:
        md.version(pkg)
    except md.PackageNotFoundError:
        return False
    return True
