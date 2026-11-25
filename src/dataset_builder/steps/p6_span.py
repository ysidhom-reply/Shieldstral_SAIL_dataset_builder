"""P6 helpers: where the inserted text sits, which tokens are protected, and the rule checks.

Pure functions, no model: they run (and are tested) on any machine.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from dataset_builder.config import Config

FENCE = "```"
WORD = re.compile(r"[A-Za-z0-9_']+")


class SpanError(ValueError):
    """The inserted text is not where the row says it is."""


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


def locate_span(row: dict[str, Any], benign_text: str | None = None) -> Span:
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


def doc_key(row: dict[str, Any]) -> str:
    """Identity of the clean document of a row: the row's `doc_id` if present, else the
    document with the inserted text removed (a positive and its negative share it only through
    `parent_id`, so the key is made from the row's own insertion)."""
    if row.get("doc_id"):
        return str(row["doc_id"])
    import hashlib

    doc, idx = row["document"], row["insert_index"]
    inserted = row["injection_span"] if row["label"] == 1 else None
    span = row.get("benign_span")
    if inserted is None and span:
        inserted = doc[span[0] : span[1]]
    clean = doc[:idx] + doc[idx + len(inserted or "") :]
    return hashlib.sha1(clean.encode()).hexdigest()[:16]


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
