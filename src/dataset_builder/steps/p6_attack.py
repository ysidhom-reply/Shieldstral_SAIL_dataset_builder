"""P6 attack: TextAttack's TextFooler recipe on the inserted span, against a victim `Scorer`.

TextAttack only sees the span text. The victim wrapper puts each candidate back into the document
at the span's position and scores the WHOLE document (instruct + query + document) with base
Shieldstral. Only words outside the protected tokens may change, and at most `max_span_fraction`
of the span's words.

TextAttack (and its TensorFlow sentence encoder) live in their own environment (.venv-p6), see
requirements/p6-textfooler.txt. The import is lazy: tests use a fake `Engine`.

TextAttack imports flair at start-up, and flair does not work with transformers 5 (which
Shieldstral needs). TextFooler never uses flair (its part-of-speech check uses NLTK), so when flair
cannot be imported a stand-in module is registered that fails loudly if anything calls it.
"""

import importlib.metadata
import logging
import os
import sys
import types
from collections.abc import Sequence
from typing import Any, Protocol

from dataset_builder.config import TextFoolerCfg
from dataset_builder.steps.p6_span import Span, modifiable_indices
from dataset_builder.steps.p6_victim import CachedScorer, Item

logger = logging.getLogger(__name__)

RUN_HINT = ".venv-p6/Scripts/dataset-builder p6-textfooler (see requirements/p6-textfooler.txt)"


def flair_stand_in() -> bool:
    """Register a stand-in `flair` if the real one cannot be imported. True if registered."""
    try:
        import flair  # noqa: F401
    except Exception:  # flair fails with ImportError or AttributeError
        for name in [m for m in sys.modules if m == "flair" or m.startswith("flair.")]:
            del sys.modules[name]
    else:
        return False

    class Unavailable:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("flair is not available here; TextFooler uses the NLTK tagger")

        @classmethod
        def load(cls, *a: Any, **k: Any) -> Any:
            cls()

    root = types.ModuleType("flair")
    data = types.ModuleType("flair.data")
    models = types.ModuleType("flair.models")
    data.Sentence = Unavailable  # type: ignore[attr-defined]
    data.Tokenizer = type("Tokenizer", (), {})  # type: ignore[attr-defined]  # a base class
    models.SequenceTagger = Unavailable  # type: ignore[attr-defined]
    root.device = None  # type: ignore[attr-defined]
    root.data, root.models = data, models  # type: ignore[attr-defined]
    sys.modules.update({"flair": root, "flair.data": data, "flair.models": models})
    return True


class Engine(Protocol):
    """Attacks one span. `attack` returns the final span text, the number of victim queries and
    p(yes) of the original and of the final text. `score` is p(yes) of whole samples."""

    def attack(
        self,
        span: Span,
        label: int,
        tokens: list[str],
        instruct: str,
        query: str,
        feedback: bool = True,
    ) -> dict[str, Any]: ...

    def score(self, items: Sequence[Item]) -> list[float]: ...

    def info(self) -> dict[str, Any]: ...


def boundary_shift(p: float, threshold: float) -> float:
    """Move p so that TextAttack's argmax boundary (0.5) sits at `threshold`."""
    return min(max(p - threshold + 0.5, 0.0), 1.0)


class TextFoolerEngine:
    def __init__(self, tf: TextFoolerCfg, scorer: CachedScorer) -> None:
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        if flair_stand_in():
            logger.info("flair not importable: stand-in registered (TextFooler does not use it)")
        try:
            import numpy as np
            import textattack  # noqa: F401
            from textattack import Attack
            from textattack.attack_recipes import TextFoolerJin2019
            from textattack.constraints import PreTransformationConstraint
            from textattack.constraints.overlap import MaxWordsPerturbed
            from textattack.constraints.semantics import WordEmbeddingDistance
            from textattack.constraints.semantics.sentence_encoders import (
                UniversalSentenceEncoder,
            )
            from textattack.models.wrappers import ModelWrapper
        except ImportError as e:  # pragma: no cover - environment message
            raise RuntimeError(f"P6 needs TextAttack ({e}). Run:\n  {RUN_HINT}") from e

        self.tf, self.scorer = tf, scorer
        self.ctx: dict[str, Any] = {"span": None, "tokens": [], "q": ("", ""), "feedback": True}
        ctx = self.ctx

        class Victim(ModelWrapper):
            def __init__(self) -> None:
                self.model = None

            def __call__(self, text_list: list[str]) -> Any:
                if not ctx["feedback"]:  # no victim feedback: a constant, never-flipping output
                    return np.array([[0.9, 0.1]] * len(text_list))
                instruct, query = ctx["q"]
                span: Span = ctx["span"]
                ps = scorer.score([(instruct, query, span.document(t)) for t in text_list])
                qs = [boundary_shift(p, tf.threshold) for p in ps]
                return np.array([[1 - q, q] for q in qs])

        class Protect(PreTransformationConstraint):
            """Only words outside the protected tokens may be swapped."""

            def _get_modifiable_indices(self, current_text: Any) -> set[int]:
                return modifiable_indices(current_text.words, ctx["tokens"])

        recipe = TextFoolerJin2019.build(Victim())
        used = {"max_candidates": recipe.transformation.max_candidates}
        for c in recipe.constraints:
            if isinstance(c, WordEmbeddingDistance):
                used["min_cos_sim"] = c.min_cos_sim
            if isinstance(c, UniversalSentenceEncoder):
                used["use_threshold"], used["use_window"] = c.threshold, c.window_size
        for k, v in used.items():  # the recipe's values must be the ones in the config
            if abs(float(v) - float(getattr(tf, k))) > 1e-9:
                raise ValueError(f"TextFooler recipe has {k}={v}, config says {getattr(tf, k)}")
        self.constraints_used = {
            **used,
            "max_span_fraction": tf.max_span_fraction,
            "threshold": tf.threshold,
            "pre_transformation": [type(c).__name__ for c in recipe.pre_transformation_constraints]
            + ["Protect (ours)"],
            "constraints": [type(c).__name__ for c in recipe.constraints] + ["MaxWordsPerturbed"],
            "transformation": type(recipe.transformation).__name__,
            "search": type(recipe.search_method).__name__,
            "goal": type(recipe.goal_function).__name__,
        }
        self.attack_obj = Attack(
            recipe.goal_function,
            [
                *recipe.pre_transformation_constraints,
                Protect(),
                *recipe.constraints,
                MaxWordsPerturbed(max_percent=tf.max_span_fraction),
            ],
            recipe.transformation,
            recipe.search_method,
        )

    def release(self) -> None:
        """Free the victim's GPU memory before the judge phase."""
        self.scorer.release()

    def attack(
        self,
        span: Span,
        label: int,
        tokens: list[str],
        instruct: str,
        query: str,
        feedback: bool = True,
    ) -> dict[str, Any]:
        self.ctx.update(span=span, tokens=tokens, q=(instruct, query), feedback=feedback)
        res = self.attack_obj.attack(span.text, label)
        text = res.perturbed_result.attacked_text.text
        p_orig = self.scorer.score([(instruct, query, span.document())])[0]
        # the final text's score comes from our own scorer: the threshold rule is ours
        p_new = self.scorer.score([(instruct, query, span.document(text))])[0]
        return {
            "text": text,
            "queries": int(res.perturbed_result.num_queries),
            "p_orig": p_orig,
            "p_new": p_new,
        }

    def score(self, items: Sequence[Item]) -> list[float]:
        return self.scorer.score(items)

    def info(self) -> dict[str, Any]:
        versions = {}
        for pkg in ("textattack", "tensorflow", "tensorflow-hub", "transformers", "torch"):
            try:
                versions[pkg] = importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                versions[pkg] = "n/a"
        return self.scorer.info() | {
            "victim_license": None,
            "constraints": self.constraints_used,
            "versions": versions,
        }
