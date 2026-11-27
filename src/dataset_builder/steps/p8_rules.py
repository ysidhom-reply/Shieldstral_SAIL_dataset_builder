"""P8 rule filters, applied to every LLM-written variant before the judge.

A TextFooler-style rewrite replaces words with close synonyms and keeps everything else. Unlike
TextFooler, an LLM may swap one word for two, so the word count may move a little (at most
`word_count_tolerance`). Changed words are counted on a word alignment (difflib), so an inserted or
deleted word counts as changed. The text outside the span is unchanged by construction: the
variant is put back with `Span.document(new)`. For code attacks the span is the introductory
sentence only; the code block is protected and must not appear in the rewrite.
"""

import difflib
from collections.abc import Sequence

from dataset_builder.steps.p6_span import FENCE, WORD, Span


def fit_whitespace(original: str, new: str) -> str:
    """The rewrite with the original span's leading and trailing whitespace (line breaks around
    the inserted text belong to the document, not to the rewrite)."""
    core = original.strip()
    if not core:
        return new
    lead = original[: len(original) - len(original.lstrip())]
    trail = original[len(original.rstrip()) :]
    return lead + new.strip() + trail


def changed_words(old: Sequence[str], new: Sequence[str]) -> int:
    """Words that differ between the two word lists, on a case-insensitive alignment."""
    sm = difflib.SequenceMatcher(
        a=[w.lower() for w in old], b=[w.lower() for w in new], autojunk=False
    )
    kept = sum(block.size for block in sm.get_matching_blocks())
    return max(len(old), len(new)) - kept


def check_llm_variant(
    span: Span, new_text: str, tokens: Sequence[str], cap: float, tolerance: float
) -> tuple[str | None, int, int]:
    """(reason or None, words changed, words in the span)."""
    old_w, new_w = WORD.findall(span.text), WORD.findall(new_text)
    n = len(old_w)
    if not new_text.strip() or not new_w:
        return "empty rewrite", 0, n
    if FENCE in new_text:
        return "code block in the rewrite", 0, n
    changed = changed_words(old_w, new_w)
    if changed == 0:
        return "no word changed", 0, n
    if abs(len(new_w) - n) > tolerance * n + 1e-9:
        return f"word count {n} -> {len(new_w)} (tolerance {tolerance:.0%})", changed, n
    if changed > cap * n + 1e-9:
        return f"{changed}/{n} words changed (cap {cap:.0%})", changed, n
    for tok in tokens:
        if new_text.count(tok) < span.text.count(tok):
            return f"protected token changed: {tok!r}", changed, n
    return None, changed, n
