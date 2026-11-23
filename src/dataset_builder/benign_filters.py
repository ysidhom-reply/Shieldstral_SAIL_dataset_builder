"""P3 filters 1-3 for benign texts (no API). Each check returns None (pass) or a short reason."""

import difflib
import json
import re
from dataclasses import dataclass
from typing import Any

from nltk.tokenize.punkt import PunktSentenceTokenizer

from dataset_builder.config import P3Cfg

BUCKETS = ("short", "medium", "long")
_URL = re.compile(
    r"https?://|\bwww\.|\b[\w-]+\.(com|org|net|io|gov|edu|info|biz|co\.uk|ly)\b", re.IGNORECASE
)
_URL_CODE = re.compile(r"https?://|\bwww\.", re.IGNORECASE)  # code has attribute access
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CODE_IN_TEXT = re.compile(r"`|\bdef\s+\w+\s*\(|^\s*(import|from)\s+\w+|\bprint\(", re.MULTILINE)
# The intro may contain inline code (`__module__`); only a triple backtick opens the block.
# Pilot p3-v2: 46 good code additions were rejected because the intro had inline backticks.
_CODE_SHAPE = re.compile(
    r"\s*(?P<intro>(?:(?!```).)+?)\s*```[\w+-]*\n(?P<code>.*?)\n?```\s*", re.DOTALL
)
_FENCE_LANG = re.compile(r"```[\w+-]*")
_SPACES = re.compile(r"\s+")
_WORD = re.compile(r"\w+")
_tokenizer = PunktSentenceTokenizer()


def group_of(scenario: str) -> str:
    return "code" if scenario == "code" else "text"


def normalise(text: str) -> str:
    return _SPACES.sub(" ", text.lower()).strip()


# --------------------------------------------------------------------------- filter 1: shape


def parse_texts(raw: str) -> list[str] | None:
    """The reply must be a JSON list of strings (a ```json fence around it is tolerated)."""
    s = raw.strip()
    if s.startswith("```"):
        s = _FENCE_LANG.sub("", s, count=1).strip()
        s = s.removesuffix("```").strip()
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or not all(isinstance(t, str) for t in data):
        return None
    return data


# Structured output for code (prompt B, p3-v2): one item = one addition, so the model cannot
# split the sentence from its code block, and the reply is always valid JSON.
CODE_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "code_additions",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "additions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"sentence": {"type": "string"}, "code": {"type": "string"}},
                        "required": ["sentence", "code"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["additions"],
            "additionalProperties": False,
        },
    },
}
_OUTER_FENCE = re.compile(r"^\s*```[\w+-]*\n?|\n?```\s*$")


def parse_code_additions(raw: str) -> list[str] | None:
    """Structured code reply -> addition texts: the sentence, then the code in one fenced block."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    items = data.get("additions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for it in items:
        if not (
            isinstance(it, dict)
            and isinstance(it.get("sentence"), str)
            and isinstance(it.get("code"), str)
        ):
            return None
        code = _OUTER_FENCE.sub("", it["code"].strip("\n")).strip("\n")
        out.append(f"{it['sentence'].strip()}\n```python\n{code}\n```")
    return out


def length_bucket(text: str, targets: list[int]) -> str:
    n = len(text)
    return min(zip(BUCKETS, targets, strict=True), key=lambda bt: (abs(n - bt[1]), bt[1]))[0]


def _sentences(text: str) -> int:
    return len(list(_tokenizer.span_tokenize(text.strip()))) if text.strip() else 0


def split_code(text: str) -> tuple[str, str] | None:
    """(intro, code) if the text is one intro then exactly one fenced block, else None."""
    if text.count("```") != 2:
        return None
    m = _CODE_SHAPE.fullmatch(text)
    return (m["intro"], m["code"]) if m else None


def check_shape(text: str, scenario: str, p3: P3Cfg) -> str | None:
    targets = p3.lengths[group_of(scenario)]
    lo, hi = p3.length_range[0] * targets[0], p3.length_range[1] * targets[-1]
    if not text.strip():
        return "empty"
    if not lo <= len(text) <= hi:
        return f"length {len(text)} outside {lo:.0f}-{hi:.0f}"
    if scenario == "code":
        parts = split_code(text)
        if parts is None:
            return "not one intro + exactly one fenced code block"
        intro, code = parts
        if not code.strip():
            return "empty code block"
        if "\n" in intro.strip() or _sentences(intro) > 2:
            return "intro is not one sentence"
        return None
    if "\n" in text.strip():
        return "line break in a text snippet"
    n = _sentences(text)
    if not 1 <= n <= 2:
        return f"{n} sentences (expected 1-2)"
    return None


# --------------------------------------------------------------------------- filter 2: content


@dataclass
class AttackIndex:
    """Normalised BIPIA attack texts and their k-word spans, for the overlap check."""

    texts: list[str]
    spans: set[tuple[str, ...]]
    k: int

    @classmethod
    def build(cls, attacks: list[dict[str, Any]], k: int) -> "AttackIndex":
        texts = [normalise(a["text"]) for a in attacks]
        spans = {
            tuple(w[i : i + k])
            for t in texts
            for w in [_WORD.findall(t)]
            for i in range(len(w) - k + 1)
        }
        return cls(texts=texts, spans=spans, k=k)

    def overlap(self, text: str) -> str | None:
        t = normalise(text)
        if any(a and a in t for a in self.texts):
            return "contains a BIPIA attack text"
        w = _WORD.findall(t)
        if any(tuple(w[i : i + self.k]) in self.spans for i in range(len(w) - self.k + 1)):
            return f"shares {self.k} consecutive words with a BIPIA attack"
        return None


def _phrase(words: list[str], flags: int) -> re.Pattern | None:
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", flags)


def check_forbidden(text: str, scenario: str, p3: P3Cfg, attacks: AttackIndex) -> str | None:
    if _EMAIL.search(text):  # before the URL check: "jane@corp.com" also looks like a domain
        return "email address"
    if scenario == "code":
        parts = split_code(text)
        intro, code = parts if parts else (text, "")
        prose = intro
        if _URL_CODE.search(text):
            return "URL"
    else:
        prose, code = text, ""
        if _URL.search(text):
            return "URL"
    word_lists = [
        (_phrase(p3.assistant_words, re.IGNORECASE), "assistant wording"),
        (_phrase(p3.assistant_words_case_sensitive, 0), "assistant wording"),
    ]
    if scenario != "code":  # ordinary wording in programming advice ("Instead, ...")
        word_lists.append(
            (_phrase(p3.assistant_words_text_only, re.IGNORECASE), "assistant wording")
        )
    for pattern, label in word_lists:
        if pattern and (m := pattern.search(prose)):
            return f"{label}: {m.group(0)!r}"
    if scenario == "code":
        for pat in p3.code_intro_patterns:
            if m := re.search(pat, prose, re.IGNORECASE):
                return f"request to include/run code: {m.group(0)!r}"
        for pat in p3.code_forbidden_patterns:
            if m := re.search(pat, code):
                return f"forbidden code: {m.group(0)!r}"
    elif _CODE_IN_TEXT.search(text):
        return "code in a text snippet"
    return attacks.overlap(text)


# --------------------------------------------------------------------------- filter 3: duplicates


def check_duplicate(text: str, accepted: list[str], ratio: float) -> str | None:
    t = normalise(text)
    for other in accepted:
        o = normalise(other)
        if t == o:
            return "exact duplicate"
        sm = difflib.SequenceMatcher(None, t, o)
        if sm.real_quick_ratio() >= ratio and sm.quick_ratio() >= ratio and sm.ratio() >= ratio:
            return f"near duplicate (ratio {sm.ratio():.2f})"
    return None
