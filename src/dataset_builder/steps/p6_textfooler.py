"""P6: TextFooler test variants of `bipia` test samples, against ProtectAI's guard model (CPU).

For each candidate (seeded order, per scenario and class):
1. locate the inserted span (attack or benign text) in the document; code: only the intro
   sentence is editable, the fenced block stays fixed;
2. protected tokens (emails, URLs, identifiers, inline code, numbers, named entities) are found;
3. anchor: the judge must agree with the label on the original span, else the candidate is
   excluded (no attack is run);
4. TextAttack's TextFooler recipe attacks the span; the victim scores the whole document
   (sliding windows, max injection probability);
5. a successful variant is validated by rules and by the judge, and kept only if both pass.

Every candidate's outcome is appended to data/interim/p6_results[_pilot].jsonl, so a stopped run
resumes. Runs in its own uv environment (see requirements/p6-textfooler.txt).
"""

import hashlib
import importlib.metadata
import json
import logging
import os
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from dataset_builder.config import Config, load_config
from dataset_builder.io import prepare_out, read_jsonl, read_parquet, rng, write_parquet
from dataset_builder.reports import CountMismatchError, report_path, write_counts_report
from dataset_builder.schema import Sample

logger = logging.getLogger(__name__)

SCENARIOS = ("email", "table", "code")
FENCE = "```"
WORD = re.compile(r"[A-Za-z0-9_']+")
RUN_HINT = (
    "uv run --isolated --no-project --with-editable . --with-requirements "
    "requirements/p6-textfooler.txt dataset-builder p6-textfooler"
)
# Outcomes of one candidate, in the order they are decided.
STATUSES = (
    "no_editable",  # no word of the span may be changed (all protected)
    "judge_disagrees",  # the judge disagrees with the label on the original span
    "skipped",  # the victim already gets the original wrong: no prediction to change
    "failed",  # TextFooler did not change the prediction
    "rule_rejected",  # succeeded, but a rule check failed
    "judge_rejected",  # succeeded, but the judge no longer agrees with the label
    "kept",
)


class SpanError(ValueError):
    """The inserted text is not where the row says it is."""


# ---------------------------------------------------------------- span and protection


@dataclass(frozen=True)
class Span:
    prefix: str  # document before the editable text
    text: str  # the editable text (TextAttack only sees this)
    suffix: str  # document after it (includes the fixed code block for code)
    code: str  # the fixed code part of the inserted text ('' if none)

    def document(self, text: str | None = None) -> str:
        return self.prefix + (self.text if text is None else text) + self.suffix

    def inserted(self, text: str | None = None) -> str:
        """The whole inserted text (what the judge reads)."""
        return (self.text if text is None else text) + self.code


def split_intro(inserted: str) -> tuple[str, str]:
    """(intro, code): code attacks are an intro sentence followed by a fenced block."""
    i = inserted.find(FENCE)
    return (inserted, "") if i < 0 else (inserted[:i], inserted[i:])


def locate_span(row: dict[str, Any], benign_text: str | None) -> Span:
    """The inserted text of a row: the attack (label 1) or the benign text (label 0), found
    through `benign_span` when the row has it (else the pool text at `insert_index`)."""
    doc, idx = row["document"], row["insert_index"]
    inserted = row["injection_span"] if row["label"] == 1 else benign_text
    span = row.get("benign_span") if row["label"] == 0 else None
    if span:
        idx, inserted_at = span[0], doc[span[0] : span[1]]
        if benign_text is not None and inserted_at != benign_text:
            raise SpanError(f"{row['sample_id']}: benign_span does not hold the pool text")
        inserted = inserted_at
    if not inserted:
        raise SpanError(f"{row['sample_id']}: no inserted text")
    if doc[idx : idx + len(inserted)] != inserted:
        raise SpanError(f"{row['sample_id']}: inserted text not found at insert_index {idx}")
    intro, code = split_intro(inserted) if row["scenario"] == "code" else (inserted, "")
    return Span(prefix=doc[:idx], text=intro, suffix=doc[idx + len(intro) :], code=code)


PROTECT_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "url": re.compile(r"\b(?:https?://|www\.)\S+"),
    "domain": re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:[a-z]{2,6})\b"),
    "inline_code": re.compile(r"`[^`\n]+`"),
    "call": re.compile(r"\b[A-Za-z_][\w.]*\([^()\n]*\)"),
    "dotted": re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b"),
    "snake": re.compile(r"\b[A-Za-z]\w*_\w+\b"),
    "camel": re.compile(r"\b[a-z]+[A-Z]\w*\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b"),
    "number": re.compile(r"\b\d[\d,.:/-]*\b"),
}


def protected_tokens(text: str, entities: Callable[[str], list[str]] | None = None) -> list[str]:
    """Tokens TextFooler must not change: rule-based patterns plus named entities."""
    found: list[tuple[int, str]] = []
    for rx in PROTECT_PATTERNS.values():
        found += [(m.start(), m.group(0)) for m in rx.finditer(text)]
    if entities:
        for ent in entities(text):
            i = text.find(ent)
            if i >= 0:
                found.append((i, ent))
    out: list[str] = []
    for _, tok in sorted(found):
        tok = tok.rstrip(".,;:")
        if tok and tok not in out:
            out.append(tok)
    return out


def protected_words(tokens: Sequence[str]) -> set[str]:
    return {w.lower() for tok in tokens for w in WORD.findall(tok)}


def modifiable_indices(words: Sequence[str], tokens: Sequence[str]) -> set[int]:
    """Indices of the words that may be swapped: every word not part of a protected token."""
    blocked = protected_words(tokens)
    return {i for i, w in enumerate(words) if w.lower() not in blocked}


def nltk_entities(cfg: Config) -> Callable[[str], list[str]]:
    """Named entities with NLTK's ne_chunk (data downloaded once into data/raw/nltk_data)."""
    import nltk

    data = cfg.paths.raw / "nltk_data"
    if str(data) not in nltk.data.path:
        nltk.data.path.insert(0, str(data))
    for pkg in ("punkt_tab", "averaged_perceptron_tagger_eng", "maxent_ne_chunker_tab", "words"):
        nltk.download(pkg, download_dir=str(data), quiet=True)

    def ents(text: str) -> list[str]:
        tree = nltk.ne_chunk(nltk.pos_tag(nltk.word_tokenize(text)))
        return [
            " ".join(tok for tok, _ in st.leaves())
            for st in tree
            if hasattr(st, "label")
            and st.label() in {"PERSON", "ORGANIZATION", "GPE", "LOCATION", "FACILITY", "GSP"}
        ]

    return ents


# ---------------------------------------------------------------- rule checks


def check_variant(
    span: Span, new_text: str, tokens: Sequence[str], max_fraction: float
) -> tuple[str | None, int, int]:
    """(reason or None, words changed, words in the span). Only word swaps are allowed."""
    old_w, new_w = WORD.findall(span.text), WORD.findall(new_text)
    n = len(old_w)
    if len(new_w) != n:
        return "word count changed", 0, n
    changed = sum(a != b for a, b in zip(old_w, new_w, strict=True))
    if changed == 0:
        return "no word changed", 0, n
    if changed > max_fraction * n + 1e-9:
        return f"{changed}/{n} words changed (cap {max_fraction:.0%})", changed, n
    if WORD.sub("", span.text) != WORD.sub("", new_text):
        return "punctuation or spacing changed", changed, n
    for tok in tokens:
        if new_text.count(tok) < span.text.count(tok):
            return f"protected token changed: {tok!r}", changed, n
    return None, changed, n


# ---------------------------------------------------------------- victim (sliding windows)


class WindowScorer:
    """Max injection probability over windows of `window` tokens (stride `stride`).

    `score_windows` takes token-id lists (without special tokens) and returns one probability
    each. Window scores are cached by their token ids: windows that do not overlap the span are
    identical across TextFooler's queries, so they are scored once. Exact, no approximation.
    """

    def __init__(
        self,
        encode: Callable[[list[str]], list[list[int]]],
        score_windows: Callable[[list[list[int]]], list[float]],
        window: int,
        stride: int,
        n_special: int = 2,
        batch_size: int = 16,
    ) -> None:
        self.encode, self.score_windows = encode, score_windows
        self.size, self.stride, self.batch_size = window - n_special, stride, batch_size
        self.cache: dict[tuple[int, ...], float] = {}
        self.windows_scored = 0
        self.cache_hits = 0

    def windows(self, ids: list[int]) -> list[tuple[int, ...]]:
        out, start = [], 0
        while True:
            out.append(tuple(ids[start : start + self.size]))
            if start + self.size >= len(ids):
                return out
            start += self.stride

    def __call__(self, texts: list[str]) -> list[float]:
        per_text = [self.windows(ids) for ids in self.encode(texts)]
        todo = list(dict.fromkeys(w for ws in per_text for w in ws if w not in self.cache))
        self.cache_hits += sum(len(ws) for ws in per_text) - len(todo)
        for i in range(0, len(todo), self.batch_size):
            batch = todo[i : i + self.batch_size]
            for w, p in zip(batch, self.score_windows([list(w) for w in batch]), strict=True):
                self.cache[w] = p
            self.windows_scored += len(batch)
        if len(self.cache) > 200_000:  # bound memory on long runs
            self.cache.clear()
        return [max(self.cache[w] for w in ws) for ws in per_text]


# ---------------------------------------------------------------- attack engine


class Engine(Protocol):
    """Attacks one span. Returns status 'skipped' | 'failed' | 'succeeded', the new text,
    the number of victim queries and the injection probabilities before and after."""

    def attack(self, span: Span, label: int, tokens: list[str]) -> dict[str, Any]: ...

    def info(self) -> dict[str, Any]: ...


class TextFoolerEngine:
    """TextAttack's TextFooler recipe against the windowed victim, plus our two constraints."""

    def __init__(self, cfg: Config) -> None:
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        try:
            import numpy as np
            import textattack  # noqa: F401
            import torch
            from huggingface_hub import HfApi
            from textattack import Attack
            from textattack.attack_recipes import TextFoolerJin2019
            from textattack.constraints import PreTransformationConstraint
            from textattack.constraints.overlap import MaxWordsPerturbed
            from textattack.constraints.semantics import WordEmbeddingDistance
            from textattack.constraints.semantics.sentence_encoders import (
                UniversalSentenceEncoder,
            )
            from textattack.models.wrappers import ModelWrapper
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as e:  # pragma: no cover - environment message
            raise RuntimeError(
                f"P6 needs TextAttack and TensorFlow ({e}). Run:\n  {RUN_HINT}"
            ) from e

        tf = cfg.textfooler
        self.revision = tf.victim_revision or HfApi().model_info(tf.victim_model, token=False).sha
        if not tf.victim_revision:
            logger.warning(
                "victim revision not pinned: using %s (pin it in the config)", self.revision
            )
        torch.set_num_threads(os.cpu_count() or 4)
        tok = AutoTokenizer.from_pretrained(tf.victim_model, revision=self.revision)
        model = AutoModelForSequenceClassification.from_pretrained(
            tf.victim_model, revision=self.revision
        ).eval()
        labels = {i: str(lab).upper() for i, lab in model.config.id2label.items()}
        inj = next(i for i, lab in labels.items() if "INJ" in lab)

        def encode(texts: list[str]) -> list[list[int]]:
            return tok(texts, add_special_tokens=False)["input_ids"]

        def score_windows(windows: list[list[int]]) -> list[float]:
            seqs = [tok.build_inputs_with_special_tokens(w) for w in windows]
            width = max(len(s) for s in seqs)
            pad = tok.pad_token_id
            ids = torch.tensor([s + [pad] * (width - len(s)) for s in seqs])
            mask = torch.tensor([[1] * len(s) + [0] * (width - len(s)) for s in seqs])
            with torch.no_grad():
                probs = model(input_ids=ids, attention_mask=mask).logits.softmax(-1)
            return probs[:, inj].tolist()

        self.scorer = WindowScorer(
            encode,
            score_windows,
            tf.window,
            tf.stride,
            n_special=tok.num_special_tokens_to_add(False),
            batch_size=tf.batch_size,
        )
        self.context: dict[str, Any] = {"span": None, "tokens": []}
        scorer, context = self.scorer, self.context

        class Victim(ModelWrapper):
            def __init__(self) -> None:
                self.model = model

            def __call__(self, text_list: list[str]) -> Any:
                span: Span = context["span"]
                p = scorer([span.document(t) for t in text_list])
                return np.array([[1 - x, x] for x in p])

        class Protect(PreTransformationConstraint):
            """Only words outside the protected tokens may be swapped."""

            def _get_modifiable_indices(self, current_text: Any) -> set[int]:
                return modifiable_indices(current_text.words, context["tokens"])

        recipe = TextFoolerJin2019.build(Victim())
        # The recipe's values must be the ones in the config (they are reported).
        used = {"max_candidates": recipe.transformation.max_candidates}
        for c in recipe.constraints:
            if isinstance(c, WordEmbeddingDistance):
                used["min_cos_sim"] = c.min_cos_sim
            if isinstance(c, UniversalSentenceEncoder):
                used["use_threshold"], used["use_window"] = c.threshold, c.window_size
        for k, v in used.items():
            if abs(float(v) - float(getattr(tf, k))) > 1e-9:
                raise ValueError(f"TextFooler recipe has {k}={v}, config says {getattr(tf, k)}")
        self.constraints_used = {
            **used,
            "max_span_fraction": tf.max_span_fraction,
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
        self.victim_id = f"{tf.victim_model}@{self.revision}"

    def attack(self, span: Span, label: int, tokens: list[str]) -> dict[str, Any]:
        from textattack.attack_results import SkippedAttackResult, SuccessfulAttackResult

        self.context["span"], self.context["tokens"] = span, tokens
        res = self.attack_obj.attack(span.text, label)
        status = (
            "skipped"
            if isinstance(res, SkippedAttackResult)
            else "succeeded"
            if isinstance(res, SuccessfulAttackResult)
            else "failed"
        )
        return {
            "status": status,
            "text": res.perturbed_result.attacked_text.text,
            "queries": int(res.perturbed_result.num_queries),
            "p_orig": float(res.original_result.raw_output[1]),
            "p_new": float(res.perturbed_result.raw_output[1]),
        }

    def info(self) -> dict[str, Any]:
        versions = {}
        for pkg in ("textattack", "tensorflow", "tensorflow-hub", "transformers", "torch"):
            try:
                versions[pkg] = importlib.metadata.version(pkg)
            except importlib.metadata.PackageNotFoundError:
                versions[pkg] = "n/a"
        return {
            "victim": self.victim_id,
            "versions": versions,
            "constraints": self.constraints_used,
            "windows_scored": self.scorer.windows_scored,
            "window_cache_hits": self.scorer.cache_hits,
            "cpu_threads": os.cpu_count(),
        }


# ---------------------------------------------------------------- candidates and loop


def buckets(rows: list[dict[str, Any]], cfg: Config) -> dict[tuple[str, int], list[dict]]:
    """Test rows per (scenario, label), each shuffled with the step's seed."""
    out: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        out[(r["scenario"], int(r["label"]))].append(r)
    for key, items in out.items():
        items.sort(key=lambda r: r["sample_id"])
        rng(f"p6:{key[0]}:{key[1]}", cfg).shuffle(items)
    return dict(out)


def pilot_order(bks: dict[tuple[str, int], list[dict]], n: int) -> list[dict]:
    """n candidates, round-robin over the buckets (stratified by scenario and class)."""
    keys = [(s, lab) for s in SCENARIOS for lab in (1, 0) if (s, lab) in bks]
    out, i = [], 0
    while len(out) < n and any(i < len(bks[k]) for k in keys):
        out += [bks[k][i] for k in keys if i < len(bks[k])][: n - len(out)]
        i += 1
    return out


def process(
    row: dict[str, Any],
    benign: dict[str, str],
    engine: Engine,
    judge: Callable[[str, str], bool],
    entities: Callable[[str], list[str]] | None,
    max_fraction: float,
) -> dict[str, Any]:
    """One candidate, end to end. Returns the record appended to p6_results.jsonl."""
    t0 = time.monotonic()
    label = int(row["label"])
    span = locate_span(row, benign.get(row.get("benign_id") or ""))
    tokens = protected_tokens(span.text, entities)
    rec: dict[str, Any] = {
        "sample_id": row["sample_id"],
        "scenario": row["scenario"],
        "label": label,
        "span": span.text,
        "protected_tokens": tokens,
        "n_words": len(WORD.findall(span.text)),
    }

    def done(status: str, **kw: Any) -> dict[str, Any]:
        return rec | kw | {"status": status, "seconds": round(time.monotonic() - t0, 3)}

    if not modifiable_indices(WORD.findall(span.text), tokens):
        return done("no_editable")
    if judge(span.inserted(), row["scenario"]) != (label == 1):
        return done("judge_disagrees")
    out = engine.attack(span, label, tokens)
    base = {"queries": out["queries"], "p_orig": out["p_orig"], "p_new": out["p_new"]}
    if out["status"] in ("skipped", "failed"):
        return done(out["status"], **base)
    new = out["text"]
    reason, changed, _ = check_variant(span, new, tokens, max_fraction)
    base |= {"new_span": new, "words_changed": changed}
    if reason:
        return done("rule_rejected", reason=reason, **base)
    if judge(span.inserted(new), row["scenario"]) != (label == 1):
        return done("judge_rejected", **base)
    return done("kept", **base)


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    return {r["sample_id"]: r for r in read_jsonl(path)} if path.is_file() else {}


def append_result(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run_loop(
    order: Callable[[], list[dict]] | None,
    bks: dict[tuple[str, int], list[dict]],
    targets: dict[str, int],
    results: dict[str, dict[str, Any]],
    path: Path,
    work: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Pilot: process the fixed `order`. Full: round-robin over the buckets until every bucket
    has its target of kept variants (or runs out of candidates). Resumes from `results`."""

    def kept(key: tuple[str, int]) -> int:
        return sum(
            1
            for r in results.values()
            if (r["scenario"], r["label"]) == key and r["status"] == "kept"
        )

    last_log = time.monotonic()
    if order is not None:
        for row in order():
            if row["sample_id"] not in results:
                rec = work(row)
                results[rec["sample_id"]] = rec
                append_result(path, rec)
        return
    pos = dict.fromkeys(bks, 0)
    while True:
        active = [k for k in bks if kept(k) < targets.get(k[0], 0) and pos[k] < len(bks[k])]
        if not active:
            return
        for key in active:
            if kept(key) >= targets.get(key[0], 0):
                continue
            while pos[key] < len(bks[key]) and bks[key][pos[key]]["sample_id"] in results:
                pos[key] += 1
            if pos[key] >= len(bks[key]):
                continue
            row = bks[key][pos[key]]
            pos[key] += 1
            rec = work(row)
            results[rec["sample_id"]] = rec
            append_result(path, rec)
        if time.monotonic() - last_log > 60:
            last_log = time.monotonic()
            logger.info(
                "P6 kept: %s | processed %d",
                ", ".join(
                    f"{s}/{lab}={kept((s, lab))}/{targets.get(s, 0)}" for s, lab in sorted(bks)
                ),
                len(results),
            )


# ---------------------------------------------------------------- assembly


def variant_id(source_id: str) -> str:
    return "tf-" + hashlib.sha256(source_id.encode()).hexdigest()[:16]


def assemble(
    results: dict[str, dict[str, Any]],
    sources: dict[str, dict[str, Any]],
    targets: dict[str, int],
    victim: str,
) -> tuple[list[Sample], dict[str, Any]]:
    """Kept variants as Samples, with both classes equal per scenario (first in attack order)."""
    kept: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in results.values():  # insertion order = attack order
        if r["status"] == "kept":
            kept[(r["scenario"], r["label"])].append(r)
    samples: list[Sample] = []
    balance: dict[str, Any] = {}
    for s in SCENARIOS:
        n = min(len(kept[(s, 1)]), len(kept[(s, 0)]), targets.get(s, 0))
        balance[s] = {"kept_1": len(kept[(s, 1)]), "kept_0": len(kept[(s, 0)]), "used": n}
        for lab in (1, 0):
            for r in kept[(s, lab)][:n]:
                src = sources[r["sample_id"]]
                span = locate_span(src, None) if lab == 1 else None
                prefix_len = src["insert_index"]
                old, new = r["span"], r["new_span"]
                doc = src["document"]
                if doc[prefix_len : prefix_len + len(old)] != old:
                    raise CountMismatchError(f"{r['sample_id']}: span moved")
                document = doc[:prefix_len] + new + doc[prefix_len + len(old) :]
                fields = {k: src[k] for k in Sample.model_fields if k in src}
                fields |= {
                    "sample_id": variant_id(src["sample_id"]),
                    "parent_id": src["sample_id"],
                    "source": "bipia_textfooler",
                    "document": document,
                    "label_provenance": "validated",
                    "generation_method": "textfooler",
                    "protected_tokens": r["protected_tokens"],
                    "victim_model": victim,
                    "injection_span": (new + span.code) if span else None,
                }
                if lab == 0 and src.get("benign_span"):
                    # the benign text (perturbed intro + unchanged code) may change length
                    start, end = src["benign_span"]
                    fields["benign_span"] = [start, end + len(new) - len(old)]
                samples.append(Sample(**fields))
    return samples, balance


# ---------------------------------------------------------------- report


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _rate(a: float, b: float) -> str:
    return f"{a / b:.1%}" if b else "n/a"


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ⏎ ")


def outcome_stats(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per (scenario, label) and per class: outcome counts, success rate, words, queries."""

    def summarise(rs: list[dict[str, Any]]) -> dict[str, Any]:
        c = Counter(r["status"] for r in rs)
        succeeded = [r for r in rs if r["status"] in ("rule_rejected", "judge_rejected", "kept")]
        attacked = succeeded + [r for r in rs if r["status"] == "failed"]
        kept = [r for r in rs if r["status"] == "kept"]
        return {
            "processed": len(rs),
            **{s: c.get(s, 0) for s in STATUSES},
            "succeeded": len(succeeded),
            "attacked": len(attacked) + c.get("skipped", 0),
            "attack_success_rate": len(succeeded) / len(attacked) if attacked else None,
            "words_changed_mean": _mean([r["words_changed"] for r in kept]),
            "words_changed_pct_mean": _mean(
                [r["words_changed"] / r["n_words"] for r in kept if r["n_words"]]
            ),
            "queries_per_success": _mean([r["queries"] for r in succeeded]),
            "queries_per_attack": _mean([r["queries"] for r in attacked]),
            "seconds_per_candidate": _mean([r["seconds"] for r in rs]),
            "keep_rate": len(kept) / len(rs) if rs else None,
        }

    out: dict[str, Any] = {"by_bucket": {}, "by_class": {}}
    for s in SCENARIOS:
        for lab in (1, 0):
            rs = [r for r in results if r["scenario"] == s and r["label"] == lab]
            if rs:
                out["by_bucket"][f"{s}/{lab}"] = summarise(rs)
    for lab in (1, 0):
        out["by_class"][str(lab)] = summarise([r for r in results if r["label"] == lab])
    out["all"] = summarise(results)
    return out


def projection(stats: dict[str, Any], targets: dict[str, int]) -> dict[str, Any]:
    """Projected full-run time from the pilot: attempts needed per bucket x seconds each.
    A bucket with no kept variant in the pilot uses its class's keep rate."""
    rows, total = [], 0.0
    for key, st in stats["by_bucket"].items():
        s, lab = key.split("/")
        rate = st["keep_rate"] or stats["by_class"][lab]["keep_rate"]
        need = targets.get(s, 0)
        attempts = need / rate if rate else None
        secs = attempts * st["seconds_per_candidate"] if attempts is not None else None
        total += secs or 0
        rows.append(
            {
                "bucket": key,
                "target": need,
                "keep_rate": rate,
                "attempts": attempts,
                "hours": secs / 3600 if secs is not None else None,
            }
        )
    unknown = any(r["hours"] is None for r in rows)
    return {"rows": rows, "total_hours": None if unknown else total / 3600}


def _sections(
    cfg: Config,
    results: list[dict[str, Any]],
    stats: dict[str, Any],
    info: dict[str, Any],
    is_pilot: bool,
    extra: dict[str, Any],
) -> list[tuple[str, str]]:
    tf = cfg.textfooler
    mode = f"pilot ({len(results)} candidates, nothing written to data/out)" if is_pilot else "full"
    out: list[tuple[str, str]] = []
    out.append(
        (
            "Run",
            "\n".join(
                [
                    f"- Mode: {mode}; seed {cfg.seed}.",
                    f"- Victim: `{info.get('victim')}` on CPU ({info.get('cpu_threads')} threads); "
                    f"input = the document only; windows of {tf.window} tokens, stride "
                    f"{tf.stride}; score = max injection probability.",
                    f"- Judge: `{extra.get('judge')}` (judge-v3 policy prompt), one text per call.",
                    "- Versions: "
                    + ", ".join(f"{k} {v}" for k, v in info.get("versions", {}).items()),
                    f"- Victim windows scored: {info.get('windows_scored', 0):,}; served from the "
                    f"window cache: {info.get('window_cache_hits', 0):,}.",
                    f"- Wall time: {extra.get('wall_seconds', 0) / 60:.1f} min; Mistral cost "
                    f"${extra.get('cost_usd', 0):.4f}.",
                ]
            ),
        )
    )
    c = info.get("constraints", {})
    out.append(
        (
            "TextFooler constraint values",
            _table(
                ["setting", "value"],
                [
                    ["recipe", "TextAttack `TextFoolerJin2019` (MIT)"],
                    [
                        "transformation",
                        f"{c.get('transformation')} (counter-fitted embeddings), "
                        f"{c.get('max_candidates')} synonym candidates per word",
                    ],
                    ["word-embedding cosine similarity", f">= {c.get('min_cos_sim')}"],
                    [
                        "sentence similarity (USE, angular)",
                        f">= {c.get('use_threshold')}, window {c.get('use_window')} words (the "
                        "recipe skips spans shorter than the window)",
                    ],
                    [
                        "maximum fraction of words perturbed",
                        f"{c.get('max_span_fraction', tf.max_span_fraction):.0%} of the span's "
                        "words (ours)",
                    ],
                    ["pre-transformation constraints", ", ".join(c.get("pre_transformation", []))],
                    ["constraints", ", ".join(c.get("constraints", []))],
                    ["search / goal", f"{c.get('search')} / {c.get('goal')}"],
                ],
            ),
        )
    )
    rows = []
    for key, st in stats["by_bucket"].items():
        rows.append(
            [
                key,
                st["processed"],
                *[st[s] for s in STATUSES],
                _rate(st["succeeded"], st["succeeded"] + st["failed"]),
            ]
        )
    out.append(
        (
            "Outcomes per scenario and class",
            _table(["scenario/label", "processed", *STATUSES, "attack success rate"], rows)
            + "\n\n*Attack success rate* = prediction changed / (changed + failed). "
            "`skipped` = the victim already got the original wrong; "
            "`judge_disagrees` = the judge's verdict on the original span differs from the "
            "label, so the candidate is not attacked.",
        )
    )
    rows = []
    for name, st in [
        ("label 1 (attacked)", stats["by_class"]["1"]),
        ("label 0 (benign)", stats["by_class"]["0"]),
        ("all", stats["all"]),
    ]:
        acc = 1 - st["skipped"] / st["attacked"] if st["attacked"] else None
        rows.append(
            [
                name,
                _rate(st["succeeded"], st["succeeded"] + st["failed"]),
                f"{acc:.1%}" if acc is not None else "n/a",
                f"{st['words_changed_mean']:.2f} ({st['words_changed_pct_mean']:.1%})",
                f"{st['queries_per_success']:.0f}",
                f"{st['seconds_per_candidate']:.1f}",
                st["kept"],
            ]
        )
    out.append(
        (
            "Attack success, words changed, queries",
            _table(
                [
                    "class",
                    "attack success rate vs ProtectAI",
                    "victim accuracy on originals",
                    "words changed per kept variant (% of span)",
                    "queries per success",
                    "seconds per candidate",
                    "kept",
                ],
                rows,
            ),
        )
    )
    if is_pilot:
        pj = extra["projection"]
        prow = [
            [
                r["bucket"],
                r["target"],
                _rate(r["keep_rate"] or 0, 1) if r["keep_rate"] else "n/a",
                f"{r['attempts']:.0f}" if r["attempts"] is not None else "n/a",
                f"{r['hours']:.1f}" if r["hours"] is not None else "n/a",
            ]
            for r in pj["rows"]
        ]
        total = (
            f"**{pj['total_hours']:.1f} h**"
            if pj["total_hours"] is not None
            else "**unknown** (a class had no kept variant)"
        )
        out.append(
            (
                "Projected full run",
                _table(
                    [
                        "bucket",
                        "target kept",
                        "keep rate (pilot)",
                        "candidates to process",
                        "hours",
                    ],
                    prow,
                )
                + f"\n\nProjected total: {total}. Rough: the pilot has only "
                f"{len(results)} candidates (about {len(results) // max(len(pj['rows']), 1)} per "
                "bucket).",
            )
        )
    ex = [r for r in results if r.get("new_span")][:10]
    if ex:
        lines = []
        for r in ex:
            old_w, new_w = WORD.findall(r["span"]), WORD.findall(r["new_span"])
            swaps = [f"{a} → {b}" for a, b in zip(old_w, new_w, strict=False) if a != b]
            lines += [
                f"**{r['scenario']} / label {r['label']} / {r['status']}** "
                f"(p(injection) {r.get('p_orig', 0):.2f} → {r.get('p_new', 0):.2f}, "
                f"{r.get('queries')} queries)",
                "",
                f"- before: {_esc(r['span'])}",
                f"- after: {_esc(r['new_span'])}",
                f"- swaps: {', '.join(swaps) or 'none'}"
                + (
                    f"; protected: {', '.join(r['protected_tokens'])}"
                    if r["protected_tokens"]
                    else ""
                ),
                "",
            ]
        out.append(("Examples", "\n".join(lines)))
    rej = Counter(r.get("reason", "") for r in results if r["status"] == "rule_rejected")
    if rej:
        out.append(
            (
                "Rule rejections",
                _table(["reason", "count"], [[_esc(k), v] for k, v in rej.most_common()]),
            )
        )
    if extra.get("checks"):
        out.append(("Checks", "\n".join(f"- {c}" for c in extra["checks"])))
    out.append(
        (
            "Decisions and deviations",
            "\n".join(
                [
                    "- **Span of a negative:** the benign text P4 inserted (length-matched to an "
                    "attack), at the same place; this is the brief's 'span of comparable length'.",
                    "- **Code:** only the introductory sentence is editable; the whole code "
                    "block is protected (brief).",
                    "- **Protected tokens:** emails, URLs, domains, inline code, calls, dotted "
                    "names, snake_case and CamelCase identifiers, numbers, and named entities "
                    "(NLTK `ne_chunk`). BIPIA's text attacks name no tools or tool arguments; "
                    "identifiers and calls cover them.",
                    f"- **Maximum words perturbed:** {tf.max_span_fraction:.0%} of the span (user "
                    "decision; the recipe has no cap).",
                    "- **Validation (user decision):** rules (only word swaps, cap, protected "
                    "tokens unchanged, document outside the span unchanged) plus the P3 judge, "
                    "which must still give the label's verdict. The judge must also agree with "
                    "the label on the original span, else the candidate is not attacked "
                    "(`judge_disagrees`). The LLM only judges; it never writes text.",
                    "- **Target (user decision):** 1,000 kept variants per class (email 200, "
                    "code 200, table 600: the brief's floor of 200 per class per scenario); "
                    "candidates are attacked in seeded order until the targets are reached.",
                    "- **Environment:** P6 runs in its own uv environment "
                    "(`requirements/p6-textfooler.txt`): TextAttack needs transformers<5, hence "
                    "huggingface-hub<1, while the project lock has huggingface-hub 2.x.",
                    "- **Report path:** `reports/p6/p6_textfooler.md` (one folder per step) "
                    "instead of `reports/p6_textfooler.md`.",
                ]
            ),
        )
    )
    return out


# ---------------------------------------------------------------- entry point


def run(
    cfg: Config | None = None,
    *,
    force: bool = False,
    is_pilot: bool = True,
    engine: Engine | None = None,
    client: Any = None,
    entities: Callable[[str], list[str]] | bool | None = True,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    tf = cfg.textfooler
    t0 = time.monotonic()
    from dataset_builder.mistral_client import MistralClient
    from dataset_builder.steps.p3_benign_pool import judge_one, load_prompts

    rows = read_parquet(cfg.paths.out / "bipia" / "test.parquet")
    sources = {r["sample_id"]: r for r in rows}
    needed = {r["benign_id"] for r in rows if r.get("benign_id")}
    benign = {
        b["benign_id"]: b["text"]
        for b in read_jsonl(cfg.paths.interim / "benign_pool.jsonl")
        if b["benign_id"] in needed
    }
    bks = buckets(rows, cfg)

    client = client or MistralClient(cfg.paths.interim / "p6_cache", max_retries=cfg.p3.max_retries)
    judge_model = client.resolve_model(cfg.benign_check.judge_model)["id"]
    prompts = load_prompts(cfg)

    def judge(text: str, scenario: str) -> bool:
        flagged, _, c = judge_one(client, judge_model, text, scenario, prompts)
        client.usage.add(judge_model, c)
        return flagged

    ents = nltk_entities(cfg) if entities is True else (entities or None)
    engine = engine or TextFoolerEngine(cfg)
    name = "p6_results_pilot.jsonl" if is_pilot else "p6_results.jsonl"
    path = cfg.paths.interim / name
    results = load_results(path)
    if results:
        logger.info("P6: resuming, %d candidates already processed", len(results))

    def work(row: dict[str, Any]) -> dict[str, Any]:
        rec = process(row, benign, engine, judge, ents, tf.max_span_fraction)
        logger.info(
            "P6 %s %s/%s: %s (%.1f s, %s queries)",
            rec["sample_id"],
            rec["scenario"],
            rec["label"],
            rec["status"],
            rec["seconds"],
            rec.get("queries", "-"),
        )
        return rec

    order = (lambda: pilot_order(bks, tf.pilot_size)) if is_pilot else None
    run_loop(order, bks, tf.targets, results, path, work)

    res_list = list(results.values())
    if is_pilot:
        wanted = {r["sample_id"] for r in pilot_order(bks, tf.pilot_size)}
        res_list = [r for r in res_list if r["sample_id"] in wanted]
    stats = outcome_stats(res_list)
    info = engine.info()
    cost = sum(client.usage.cost(cfg.p3.prices).values()) if client.usage.by_model else 0.0
    extra: dict[str, Any] = {
        "judge": judge_model,
        "wall_seconds": time.monotonic() - t0,
        "cost_usd": cost,
    }
    out_path = None
    checks: list[str] = []
    payload: dict[str, Any] = {
        "mode": "pilot" if is_pilot else "full",
        "stats": stats,
        "info": info,
    }
    if is_pilot:
        extra["projection"] = payload["projection"] = projection(stats, tf.targets)
        report_rows: list[Any] = res_list
        keys: tuple[str, ...] = ("scenario", "label", "status")
    else:
        samples, balance = assemble(results, sources, tf.targets, info["victim"])
        ids = [s.sample_id for s in samples]
        if len(set(ids)) != len(ids):
            raise CountMismatchError("P6: duplicate sample_id")
        missing = [s.parent_id for s in samples if s.parent_id not in sources]
        if missing:
            raise CountMismatchError(f"P6: parent_id not in bipia test: {missing[:3]}")
        by_label = Counter(s.label for s in samples)
        if by_label[0] != by_label[1]:
            raise CountMismatchError(f"P6: classes differ {dict(by_label)}")
        short = {s: b for s, b in balance.items() if b["used"] < tf.targets.get(s, 0)}
        checks += [
            "PASSED: every variant's parent_id is a bipia test sample; sample_ids are unique",
            f"PASSED: classes are equal ({by_label[1]} / {by_label[0]})",
            "PASSED: targets reached"
            if not short
            else f"SHORTFALL: {short} (candidates exhausted)",
        ]
        payload["balance"] = balance
        out_path = prepare_out(
            cfg.paths.out / "bipia_textfooler" / "test.parquet", force=force, cfg=cfg
        )
        write_parquet(out_path, samples)
        report_rows, keys = samples, ("scenario", "label", "position")
    extra["checks"] = checks
    payload |= {"cost_usd": cost, "wall_seconds": extra["wall_seconds"]}
    report_name = "p6_textfooler_pilot" if is_pilot else "p6_textfooler"
    write_counts_report(
        "p6_textfooler",
        report_rows,
        keys=keys,
        cfg=cfg,
        sections=_sections(cfg, res_list, stats, info, is_pilot, extra),
        extra=payload,
        name=report_name,
    )
    return {
        "report": report_path(cfg, f"{report_name}.md"),
        "results": path,
        "out": out_path,
        "stats": stats,
        "projection": payload.get("projection"),
        "cost_usd": cost,
    }
