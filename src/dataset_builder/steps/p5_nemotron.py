"""P5. Nemotron test set (test only: never used for training or model selection).

One pair per Nemotron record:
  positive (label 1): the environment record that holds the injection, as a read tool returns it.
  negative (label 0): the same record with the injection (and any leftover wrapper) removed and
                      the cut sentence repaired. Nothing else changes.
Pairs are capped at `goal_cap` per attacker goal. Output: data/out/nemotron/test.parquet and
reports/p5_nemotron.md.
"""

import copy
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from dataset_builder.config import Config, load_config
from dataset_builder.io import pilot, prepare_out, rng, write_parquet
from dataset_builder.nemotron_source import HF_SPLIT, fetch_nemotron, load_records
from dataset_builder.reports import (
    CountMismatchError,
    check_expected,
    compute_counts,
    write_counts_report,
)
from dataset_builder.schema import Sample, build_query, instruct_text

logger = logging.getLogger(__name__)

STEP = "p5_nemotron"
OUT_FILE = Path("nemotron") / "test.parquet"
REVIEW_NOTES = "p5_nemotron_review_notes.md"
N_REVIEW = 20

Path_ = tuple[str | int, ...]

# Wrapper frames. The open/close markers are what the negative must never contain.
PRIORITY_OPEN, PRIORITY_CLOSE = "[PRIORITY MESSAGE]", "[/PRIORITY MESSAGE]"
URGENT_OPEN = re.compile(r"⚠\ufe0f?\s*URGENT:")
URGENT_CLOSE = "This is a critical priority."
WRAPPER_MARKERS = (
    PRIORITY_OPEN,
    PRIORITY_CLOSE,
    URGENT_CLOSE,
    "⚠️ URGENT:",
    "⚠ URGENT:",
    "Then continue with the original request",
    "I forgot to mention earlier",
    "Before completing any other tasks, you MUST",
)

_CLOSERS = "\"'’”)\\]"  # noqa: RUF001 closing quotes/brackets allowed after a sentence end
_TERMINATOR = re.compile(rf"[.!?][{_CLOSERS}]*$")
_SENT_END = re.compile(rf"[.!?][{_CLOSERS}]*(?=\s)|\n|\\n")
_GREETING = re.compile(
    r"^(hi|hello|hey|dear|good (morning|afternoon|evening))\b[^\n]*,$", re.IGNORECASE
)
_TAIL_PUNCT = re.compile(r"^[.,;:!?]+\s*")
_WS = re.compile(r"\s+")


class P5CheckError(RuntimeError):
    """A P5 check failed. The report has been written; outputs have not."""


# --------------------------------------------------------------------------- locate


def string_leaves(obj: Any, path: Path_ = ()) -> Iterator[tuple[Path_, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from string_leaves(v, (*path, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from string_leaves(v, (*path, i))


def get_at(obj: Any, path: Path_) -> Any:
    for k in path:
        obj = obj[k]
    return obj


def set_at(obj: Any, path: Path_, value: Any) -> None:
    get_at(obj, path[:-1])[path[-1]] = value


def find_spans(text: str, needle: str, normalised: bool) -> list[tuple[int, int]]:
    """Exact occurrences of `needle`, or whitespace-normalised ones (any run of whitespace)."""
    if not normalised:
        out, i = [], text.find(needle)
        while i >= 0:
            out.append((i, i + len(needle)))
            i = text.find(needle, i + len(needle))
        return out
    tokens = needle.split()
    if not tokens:
        return []
    pattern = r"\s+".join(re.escape(t) for t in tokens)
    return [m.span() for m in re.finditer(pattern, text)]


def _tokens(parts: Any) -> set[str]:
    return {t for p in parts if isinstance(p, str) for t in re.split(r"[_\W]+", p.lower()) if t}


def vector_score(vector: str, path: Path_) -> int:
    """How well a path matches the injection_vector label (lab_comments ~ lab_results.*.comments).

    Leaf equal to the vector scores highest; otherwise the number of shared name tokens, counted
    only if the leaf itself is part of the vector.
    """
    leaf = path[-1] if isinstance(path[-1], str) else ""
    if leaf == vector:
        return 100
    if not leaf or not _tokens([leaf]) <= _tokens([vector]):
        return 0
    return len(_tokens([vector]) & _tokens(path))


@dataclass
class Location:
    occurrences: list[tuple[Path_, list[tuple[int, int]]]]  # every field holding the injection
    path: Path_  # the defining occurrence
    span: tuple[int, int]
    normalised: bool
    how: str  # exact | whitespace_normalised, plus the tie-break used, if any


def locate(rec: dict[str, Any]) -> Location:
    env, inj = rec["environment"], rec["injection_text"]
    normalised = False
    occ = [(p, s) for p, s in ((p, find_spans(v, inj, False)) for p, v in string_leaves(env)) if s]
    if not occ:
        normalised = True
        occ = [
            (p, s) for p, s in ((p, find_spans(v, inj, True)) for p, v in string_leaves(env)) if s
        ]
    if not occ:
        raise P5CheckError(f"record {rec['id']}: injection_text not found in environment")
    flat = [(p, sp) for p, spans in occ for sp in spans]
    how = "whitespace_normalised" if normalised else "exact"
    if len(flat) == 1:
        return Location(occ, flat[0][0], flat[0][1], normalised, how)
    scores = [vector_score(rec["injection_vector"], p) for p, _ in flat]
    best = [f for f, s in zip(flat, scores, strict=True) if s == max(scores) and s > 0]
    if len(best) > 1:  # same vector on several records: the record the user asks about
        named = [
            f
            for f in best
            if re.search(rf"(?<![\w-]){re.escape(str(f[0][1]))}(?![\w-])", rec["user_request"])
        ]
        if len(named) == 1:
            return Location(occ, named[0][0], named[0][1], normalised, how + "+user_request_key")
    if len(best) != 1:
        raise P5CheckError(
            f"record {rec['id']}: {len(flat)} occurrences, cannot pick one by injection_vector "
            f"{rec['injection_vector']!r}: {[p for p, _ in flat]}"
        )
    return Location(occ, best[0][0], best[0][1], normalised, how + "+injection_vector")


# --------------------------------------------------------------------------- counterfactual


def extend_wrapper(text: str, start: int, end: int) -> tuple[int, int, bool]:
    """Grow (start, end) over a wrapper frame that encloses the span but lies outside it."""
    before, after = text[:start], text[end:]
    o = before.rfind(PRIORITY_OPEN)
    if o >= 0 and PRIORITY_CLOSE not in before[o:] and PRIORITY_CLOSE in after:
        return o, end + after.index(PRIORITY_CLOSE) + len(PRIORITY_CLOSE), True
    opens = list(URGENT_OPEN.finditer(before))
    if opens and URGENT_CLOSE not in before[opens[-1].start() :] and URGENT_CLOSE in after:
        return opens[-1].start(), end + after.index(URGENT_CLOSE) + len(URGENT_CLOSE), True
    return start, end, False


def _ws_rank(ws: str) -> int:
    return 3 if "\n\n" in ws else 2 if "\n" in ws else 1 if ws else 0


def _seam(left_ws: str, right_ws: str) -> str:
    best = max(left_ws, right_ws, key=_ws_rank)
    return {3: "\n\n", 2: "\n", 1: " ", 0: ""}[_ws_rank(best)]


def needs_repair(before: str, after: str) -> bool:
    b = re.sub(r"(\\n|\s)+$", "", before)
    a = after.lstrip()
    lead_bad = bool(b) and not _TERMINATOR.search(b)
    tail_bad = bool(a) and (a[0].islower() or bool(_TAIL_PUNCT.match(a)))
    return lead_bad or tail_bad


def default_action(before: str, after: str) -> str:
    last_line = before.rstrip().rsplit("\n", 1)[-1].strip()
    if _GREETING.match(last_line):
        return "keep_greeting"
    b = re.sub(r"(\\n|\s)+$", "", before)
    return "cut_lead" if b and not _TERMINATOR.search(b) else "cut_tail"


def _cut_lead(before: str) -> str:
    """Cut an unfinished lead-in back to the previous sentence end (keeping the whitespace)."""
    body = before.rstrip()
    ends = list(_SENT_END.finditer(body))
    if not ends:
        return ""
    cut = ends[-1].end()
    return before[: cut + len(before[cut:]) - len(before[cut:].lstrip())]


def _cut_tail(after: str) -> str:
    """Drop a tail that starts mid-sentence, up to the start of the next sentence."""
    stripped = after.lstrip()
    lead_ws = after[: len(after) - len(stripped)]
    m = _TAIL_PUNCT.match(stripped)
    if m:  # the tail is just the cut sentence's end mark: drop it
        return lead_ws + stripped[m.end() :]
    if stripped and stripped[0].islower():
        nxt = _SENT_END.search(stripped)
        return lead_ws + (stripped[nxt.end() :].lstrip(" ") if nxt else "")
    return after


def remove_span(text: str, start: int, end: int, action: str | None) -> str:
    """Remove text[start:end], apply the repair action, and collapse whitespace at the seam only.

    The seam is the widest whitespace found just outside the removed text (before the cut, after
    it); if there is none there, the whitespace at the edges of the removed text itself (the
    injection often carries its own blank lines); a single space at least.
    """
    before, after = text[:start], text[end:]
    removed = text[start:end]
    outside = (before[len(before.rstrip()) :], after[: len(after) - len(after.lstrip())])
    edges = (removed[: len(removed) - len(removed.lstrip())], removed[len(removed.rstrip()) :])
    seam = _seam(*outside) or _seam(*edges) or " "
    if action == "cut_lead":
        before = _cut_lead(before)
        after = _cut_tail(after)
        # the kept text now ends at a sentence end: use the whitespace that followed it
        seam = _seam(before[len(before.rstrip()) :], "") or seam
    elif action == "cut_tail":
        after = _cut_tail(after)
    elif action not in (None, "join", "keep_greeting"):
        raise ValueError(f"unknown repair action {action!r}")
    b, a = before.rstrip(), after.lstrip()
    if not b:
        return a
    if not a:
        return b
    if action == "join":
        return f"{b} {a}"
    return b + seam + a


@dataclass
class Counterfactual:
    environment: dict[str, Any]
    wrapper_outside_span: bool
    repair_needed: bool
    repair_action: str | None
    field_before: str
    field_after: str


def counterfactual(
    rec: dict[str, Any], loc: Location, repairs: dict[int, dict[str, str]]
) -> Counterfactual:
    env = copy.deepcopy(rec["environment"])
    wrapper_outside = repair_needed = False
    action: str | None = None
    for path, spans in loc.occurrences:
        text = get_at(env, path)
        for start, end in sorted(spans, reverse=True):
            s, e, wrapped = extend_wrapper(text, start, end)
            defining = path == loc.path and (start, end) == loc.span
            mid = needs_repair(text[:s], text[e:])
            act = None
            if mid:
                act = repairs.get(rec["id"], {}).get("action") or default_action(text[:s], text[e:])
            if defining:
                wrapper_outside, repair_needed, action = wrapped, mid, act
            text = remove_span(text, s, e, act)
        set_at(env, path, text)
    return Counterfactual(
        env,
        wrapper_outside,
        repair_needed,
        action,
        get_at(rec["environment"], loc.path),
        get_at(env, loc.path),
    )


def wrapper_family(rec: dict[str, Any], loc: Location) -> str:
    text = get_at(rec["environment"], loc.path)
    s, e, _ = extend_wrapper(text, *loc.span)
    framed = text[s:e]
    if PRIORITY_OPEN in framed:
        return "priority_message"
    if URGENT_OPEN.search(framed) and URGENT_CLOSE in framed:
        return "urgent_emoji"
    return "bare_goal"


# --------------------------------------------------------------------------- samples


def serialise(obj: Any) -> str:
    """What a read tool would return for the record."""
    return json.dumps(obj, ensure_ascii=False, indent=2)


def normalise_goal(goal: str) -> str:
    return _WS.sub(" ", goal).strip().rstrip(".").lower()


def goal_group(rec: dict[str, Any]) -> str:
    h = hashlib.sha1(normalise_goal(rec["goal"]).encode("utf-8")).hexdigest()[:8]
    return f"{rec['domain']}:{h}"


def _dotted(path: Path_) -> str:
    return ".".join(str(p) for p in path)


@dataclass
class Pair:
    rec: dict[str, Any]
    loc: Location
    cf: Counterfactual
    pos: Sample
    neg: Sample


def build_pair(rec: dict[str, Any], repairs: dict[int, dict[str, str]], cfg: Config) -> Pair:
    loc = locate(rec)
    cf = counterfactual(rec, loc, repairs)
    doc_key = loc.path[:2]
    text = get_at(rec["environment"], loc.path)
    common = {
        "source": "nemotron",
        "split": "test",
        "source_split": HF_SPLIT,
        "scenario": rec["domain"],
        "instruct": instruct_text(cfg),
        "query": build_query(rec["user_request"], cfg),
        "document_path": _dotted(loc.path),
        "injection_vector": rec["injection_vector"],
        "attack_category": rec["attack_category"],
        "goal_group": goal_group(rec),
        "wrapper": wrapper_family(rec, loc),
        "wrapper_outside_span": cf.wrapper_outside_span,
        "sentence_repaired": cf.repair_needed,
    }
    pos_id = f"nemotron-{rec['id']:04d}-pos"
    pos = Sample(
        sample_id=pos_id,
        document=serialise(get_at(rec["environment"], doc_key)),
        label=1,
        answer="yes",
        label_provenance="injection_metadata",
        generation_method="original",
        injection_span=text[loc.span[0] : loc.span[1]],
        **common,
    )
    neg = Sample(
        sample_id=f"nemotron-{rec['id']:04d}-neg",
        parent_id=pos_id,
        document=serialise(get_at(cf.environment, doc_key)),
        label=0,
        answer="no",
        label_provenance="construction",
        generation_method="counterfactual",
        **common,
    )
    return Pair(rec, loc, cf, pos, neg)


def apply_cap(pairs: list[Pair], cap: int, cfg: Config) -> tuple[list[Pair], dict[str, Any]]:
    """Keep at most `cap` pairs per goal_group, sampled with the seed. Keeps file order."""
    groups: dict[str, list[Pair]] = defaultdict(list)
    for p in pairs:
        groups[p.pos.goal_group].append(p)
    r = rng(f"{STEP}:goal_cap", cfg)
    drop: set[str] = set()
    capped: dict[str, dict[str, Any]] = {}
    for g in sorted(groups):
        members = groups[g]
        if len(members) > cap:
            keep = set(r.sample(range(len(members)), cap))
            drop |= {m.pos.sample_id for i, m in enumerate(members) if i not in keep}
            capped[g] = {
                "domain": members[0].rec["domain"],
                "records": len(members),
                "kept": cap,
                "goal": members[0].rec["goal"],
            }
    return [p for p in pairs if p.pos.sample_id not in drop], capped


# --------------------------------------------------------------------------- checks


def _contains(doc: str, needle: str) -> bool:
    """Case-insensitive, whitespace-normalised, on the document text and its parsed strings."""
    n = _WS.sub(" ", needle).strip().lower()
    if not n:
        return False
    hay = [doc, *(v for _, v in string_leaves(json.loads(doc)))]
    return any(n in _WS.sub(" ", h).lower() for h in hay)


def check_pair(p: Pair) -> list[str]:
    errs: list[str] = []
    rid = p.rec["id"]
    pos_obj, neg_obj = json.loads(p.pos.document), json.loads(p.neg.document)
    rel = p.loc.path[2:]
    released = get_at(p.rec["environment"], p.loc.path)
    if get_at(pos_obj, rel) != released or p.pos.injection_span not in get_at(pos_obj, rel):
        errs.append(f"{rid}: positive does not contain its full injection as released")
    if not p.loc.normalised and p.rec["injection_text"] not in get_at(pos_obj, rel):
        errs.append(f"{rid}: positive misses the exact injection_text")
    if _contains(p.neg.document, p.rec["injection_text"]):
        errs.append(f"{rid}: negative still contains the injection text")
    if _contains(p.neg.document, p.rec["goal"]):
        errs.append(f"{rid}: negative still contains the goal sentence")
    for m in WRAPPER_MARKERS:
        if _contains(p.neg.document, m):
            errs.append(f"{rid}: negative contains wrapper marker {m!r}")
    doc_key = p.loc.path[:2]
    allowed = {path[2:] for path, _ in p.loc.occurrences if path[:2] == doc_key}
    pos_leaves = dict(string_leaves(pos_obj))
    neg_leaves = dict(string_leaves(neg_obj))
    if pos_leaves.keys() != neg_leaves.keys():
        errs.append(f"{rid}: positive and negative have different fields")
    diff = {k for k in pos_leaves if pos_leaves[k] != neg_leaves.get(k)}
    if not diff or not diff <= allowed:
        errs.append(f"{rid}: fields differing within the pair {sorted(map(str, diff))}")
    if json.dumps(pos_obj, sort_keys=True) == json.dumps(neg_obj, sort_keys=True):
        errs.append(f"{rid}: negative identical to positive")
    return errs


def check_dataset(pairs: list[Pair]) -> list[str]:
    errs = [e for p in pairs for e in check_pair(p)]
    for label, docs in (
        ("positive", [p.pos.document for p in pairs]),
        ("negative", [p.neg.document for p in pairs]),
    ):
        dup = [d for d, c in Counter(docs).items() if c > 1]
        if dup:
            side = (lambda p: p.pos) if label == "positive" else (lambda p: p.neg)
            ids = [p.rec["id"] for p in pairs if side(p).document in dup]
            errs.append(f"{len(dup)} duplicate {label} documents across pairs (records {ids})")
    positives = {p.pos.document for p in pairs}
    same = [p.rec["id"] for p in pairs if p.neg.document in positives]
    if same:
        errs.append(f"negatives identical to a positive: records {same}")
    return errs


def hard_negatives(pairs: list[Pair], words: list[str]) -> list[Pair]:
    pat = re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    return [p for p in pairs if pat.search(p.neg.document)]


def check_repairs(pairs: list[Pair], repairs: dict[int, Any], full: bool) -> list[str]:
    flagged = {p.rec["id"] for p in pairs if p.cf.repair_needed}
    listed = set(repairs)
    errs = []
    if flagged - listed:
        errs.append(
            f"mid-sentence cuts not checked by hand (add to the repairs file): "
            f"{sorted(flagged - listed)}"
        )
    if full and listed - flagged:
        errs.append(f"repairs file lists records the rules do not flag: {sorted(listed - flagged)}")
    return errs


# --------------------------------------------------------------------------- report


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += [
        "| " + " | ".join(str(c).replace("|", "\\|").replace("\n", "↵") for c in r) + " |"
        for r in rows
    ]
    return "\n".join(out)


def _counter_table(name: str, c: Counter) -> str:
    return _table(
        [name, "pairs"], [[k, v] for k, v in sorted(c.items(), key=lambda kv: str(kv[0]))]
    )


def _fence(text: str) -> str:
    return "````text\n" + text + "\n````"


def _review_block(p: Pair) -> str:
    return "\n".join(
        [
            f"### Record {p.rec['id']} ({p.rec['domain']}, {p.rec['attack_category']}, "
            f"wrapper {p.pos.wrapper})",
            "",
            f"- user request: {p.rec['user_request']}",
            f"- document_path: `{p.pos.document_path}`, "
            f"injection_vector: `{p.rec['injection_vector']}`",
            f"- goal: {p.rec['goal']}",
            f"- sentence_repaired: {p.cf.repair_needed} ({p.cf.repair_action}), "
            f"wrapper_outside_span: {p.cf.wrapper_outside_span}",
            "",
            "Positive document:",
            "",
            _fence(p.pos.document),
            "",
            "Negative document:",
            "",
            _fence(p.neg.document),
            "",
        ]
    )


def _sections(ctx: dict[str, Any]) -> list[tuple[str, str]]:
    cfg: Config = ctx["cfg"]
    kept: list[Pair] = ctx["kept"]
    every: list[Pair] = ctx["pairs"]
    kept_ids = {p.rec["id"] for p in kept}
    status: list[str] = ctx["status"]
    sec: list[tuple[str, str]] = []
    sec.append(
        (
            "Status",
            "**All checks passed.**"
            if not status
            else "**STOPPED: outputs not written.**\n\n" + "\n".join(f"- {s}" for s in status),
        )
    )
    stats = ctx["stats"]
    sec.append(
        (
            "Expected vs actual (brief)",
            _table(
                ["check", "expected", "actual"],
                [[k, ctx["expected"].get(k, "-"), v] for k, v in stats.items()],
            ),
        )
    )
    sec.append(
        (
            "Pairs by field (after the cap)",
            "\n\n".join(
                [
                    _counter_table("attack_category", Counter(p.pos.attack_category for p in kept)),
                    _counter_table("wrapper", Counter(p.pos.wrapper for p in kept)),
                    _counter_table("wrapper (before cap)", Counter(p.pos.wrapper for p in every)),
                    _counter_table(
                        "sentence_repaired", Counter(p.pos.sentence_repaired for p in kept)
                    ),
                    _counter_table(
                        "wrapper_outside_span", Counter(p.pos.wrapper_outside_span for p in kept)
                    ),
                    _counter_table(
                        "injection_vector", Counter(p.pos.injection_vector for p in kept)
                    ),
                ]
            ),
        )
    )
    sec.append(
        (
            "Goal cap",
            f"Cap: {cfg.nemotron.goal_cap} pairs per goal_group "
            f"(`{{domain}}:sha1(normalised goal)[:8]`), sampled with seed {cfg.seed}. "
            f"{ctx['n_goals']} distinct goals.\n\n"
            + _table(
                ["goal_group", "domain", "records", "kept", "goal"],
                [
                    [g, v["domain"], v["records"], v["kept"], v["goal"]]
                    for g, v in ctx["capped"].items()
                ],
            ),
        )
    )
    special = [
        p
        for p in every
        if p.loc.normalised
        or len(p.loc.occurrences) > 1
        or sum(len(s) for _, s in p.loc.occurrences) > 1
        or p.cf.wrapper_outside_span
    ]
    sec.append(
        (
            "Special cases (location and wrappers)",
            _table(
                ["record", "match", "occurrences", "defining path", "wrapper outside span", "kept"],
                [
                    [
                        p.rec["id"],
                        p.loc.how,
                        "; ".join(_dotted(o) for o, _ in p.loc.occurrences),
                        p.pos.document_path,
                        p.cf.wrapper_outside_span,
                        p.rec["id"] in kept_ids,
                    ]
                    for p in special
                ],
            ),
        )
    )
    vm = [p for p in every if vector_score(p.rec["injection_vector"], p.loc.path) == 0]
    sec.append(
        (
            "Paths that do not match injection_vector by name",
            _table(
                ["record", "injection_vector", "path"],
                [[p.rec["id"], p.rec["injection_vector"], p.pos.document_path] for p in vm],
            )
            if vm
            else "None.",
        )
    )
    rep = [p for p in every if p.cf.repair_needed]
    blocks = []
    for p in rep:
        note = ctx["repairs"].get(p.rec["id"], {}).get("note", "")
        blocks += [
            f"### Record {p.rec['id']}: `{p.cf.repair_action}` (kept: {p.rec['id'] in kept_ids})",
            "",
            f"Hand check: {note}",
            "",
            "Field before:",
            "",
            _fence(p.cf.field_before),
            "",
            "Field after:",
            "",
            _fence(p.cf.field_after),
            "",
        ]
    sec.append((f"Sentence repairs ({len(rep)}), checked by hand", "\n".join(blocks) or "None."))
    hn = ctx["hard"]
    sec.append(
        (
            "Hard negatives kept",
            (
                f"Negatives containing {cfg.nemotron.hard_negative_words} (word match, "
                f"case-insensitive): **{len(ctx['hard_all'])}** before the cap, "
                f"**{len(hn)}** after. They are kept.\n\n"
                + _table(
                    ["record", "domain", "words"],
                    [
                        [
                            p.rec["id"],
                            p.rec["domain"],
                            ", ".join(
                                sorted(
                                    {
                                        m.lower()
                                        for m in re.findall(
                                            r"\b(?:"
                                            + "|".join(
                                                map(re.escape, cfg.nemotron.hard_negative_words)
                                            )
                                            + r")\b",
                                            p.neg.document,
                                            re.IGNORECASE,
                                        )
                                    }
                                )
                            ),
                        ]
                        for p in hn
                    ],
                )
            ),
        )
    )
    sec.append(("Decisions and deviations", "\n".join(f"- {d}" for d in ctx["deviations"])))
    notes_path = cfg.paths.reports / REVIEW_NOTES
    notes = (
        notes_path.read_text(encoding="utf-8").strip()
        if notes_path.is_file()
        else (f"_Not written yet: add `{REVIEW_NOTES}` after reading the pairs below._")
    )
    sec.append((f"Review: {len(ctx['review'])} random pairs read in full (notes)", notes))
    sec.append(("Review sample", "\n".join(_review_block(p) for p in ctx["review"])))
    sec.append(
        (
            "Source",
            f"`{cfg.nemotron.repo_id}` at revision `{ctx['revision']}` "
            f"(CC BY 4.0), file `{cfg.nemotron.filename}`, HF split `{HF_SPLIT}`.",
        )
    )
    return sec


# --------------------------------------------------------------------------- run


def load_repairs(cfg: Config) -> dict[int, dict[str, str]]:
    path = cfg.nemotron.sentence_repairs
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {int(k): v for k, v in data.items()}


def run(cfg: Config | None = None, force: bool = False, pilot_run: bool = False) -> dict[str, Any]:
    cfg = cfg or load_config()
    path, revision = fetch_nemotron(cfg)
    records = load_records(path)
    n_records = len(records)
    if pilot_run:
        records = pilot(records, STEP, cfg=cfg)
    repairs = load_repairs(cfg)

    pairs = [build_pair(r, repairs, cfg) for r in records]
    kept, capped = apply_cap(pairs, cfg.nemotron.goal_cap, cfg)
    samples = [s for p in kept for s in (p.pos, p.neg)]
    hard_all = hard_negatives(pairs, cfg.nemotron.hard_negative_words)
    hard = hard_negatives(kept, cfg.nemotron.hard_negative_words)

    before = compute_counts([s for p in pairs for s in (p.pos, p.neg)], ("label",))
    after = compute_counts(samples, ("label", "split"))
    stats: dict[str, Any] = {
        "records": n_records,
        "before_cap": {"total": before["total"], "label": before["label"]},
        "total": after["total"],
        "label": after["label"],
        "split": after["split"],
        "whitespace_normalised_matches": sum(p.loc.normalised for p in pairs),
        "multi_occurrence_records": sum(
            sum(len(s) for _, s in p.loc.occurrences) > 1 for p in pairs
        ),
        "wrapper_outside_span": sum(p.cf.wrapper_outside_span for p in pairs),
        "sentence_repaired": sum(p.cf.repair_needed for p in pairs),
        "capped_goal_groups": {v["domain"]: v["records"] for v in capped.values()},
        "hard_negatives": {"before_cap": len(hard_all), "after_cap": len(hard)},
    }

    expected = dict(cfg.expected_counts.get(STEP, {}))
    exp_hard = expected.pop("hard_negatives", None)
    status = check_dataset(kept) + check_repairs(pairs, repairs, full=not pilot_run)
    if not pilot_run:
        try:
            check_expected(STEP, stats, expected=expected, cfg=cfg)
        except CountMismatchError as e:
            status.append(str(e).replace("\n  ", " | "))
        if exp_hard is not None and exp_hard not in (len(hard_all), len(hard)):
            status.append(
                f"hard negatives: expected {exp_hard}, got {len(hard_all)} before the "
                f"cap and {len(hard)} after"
            )

    deviations = [
        "Document = environment[collection][key] (first two levels of the matched path), "
        "serialised with json.dumps(ensure_ascii=False, indent=2). The injection inside is left "
        "exactly as released.",
        "injection_vector is a field label, not a path: a path matches it when its leaf equals "
        "the label or the leaf's name tokens are part of it (lab_comments ~ "
        "lab_results.*.comments). Multi-occurrence records pick the best-matching path.",
        "Record 1260: both occurrences sit on `positions.*.description`, so the vector cannot "
        "decide. Tie broken by the record key named in the user request (POS101).",
        "Records 530 and 566: `fraud_flag_reason` matches `fraud_investigation_flags.*.flag_reason`"
        " better than `claims.*.flag_reason`; the injection is removed from both in the negative.",
        "Whitespace at the cut is collapsed to the widest break around it (blank line > newline >"
        " space); pre-existing whitespace elsewhere in the field is left untouched.",
        "Record 1033: the lead-in 'Customs documentation appears incomplete;' ends in ';' right "
        "before the injection and is cut back to the previous sentence end, as the rule says "
        "(no rewording to '.').",
        "parent_id is set on the negative only (= the positive's sample_id). protected_tokens is "
        "empty: TextFooler is not run on Nemotron.",
        "goal_group = '{domain}:' + first 8 hex of sha1(goal, whitespace-collapsed, lowercased, "
        "trailing '.' removed).",
        "label_provenance: injection_metadata for positives (the injection comes from the "
        "release), construction for negatives.",
        "Sentence repairs: the brief expects 12; 11 records have the injection inside a sentence "
        "(3 greetings kept: 54, 641, 812; 4 unfinished lead-ins cut back: 643, 1033, 1169, 1192; "
        "3 lead-ins that only introduce the injection cut: 1074, 1082, 1247; 1 plain join: 477). "
        "Every cut point in the 1,272 records was inspected and no twelfth case was found.",
        "Hard negatives: the brief mentions 23; 25 negatives contain 'urgent', 'the user' or "
        "'assistant' (word match, case-insensitive), before and after the cap. All 25 were read: "
        "they are natural text (e.g. 'urgent EEG', 'Urgent: Product not received', 'Teaching "
        "Assistant', 17 helpdesk tickets with 'The user reported ...'), none is injection "
        "residue. The difference most likely comes from how the brief counted.",
    ]
    review = sorted(
        rng(f"{STEP}:review", cfg).sample(kept, min(N_REVIEW, len(kept))), key=lambda p: p.rec["id"]
    )
    name = STEP + ("_pilot" if pilot_run else "")
    mode = (
        f"PILOT on {len(records)} records (nothing written to data/out)"
        if pilot_run
        else "Full run"
    )
    write_counts_report(
        STEP,
        samples,
        keys=("scenario", "split", "label"),
        cfg=cfg,
        name=name,
        preamble=[("Run", f"{mode}; seed {cfg.seed}.")],
        sections=_sections(
            {
                "cfg": cfg,
                "kept": kept,
                "pairs": pairs,
                "status": status,
                "stats": stats,
                "expected": cfg.expected_counts.get(STEP, {}),
                "capped": capped,
                "n_goals": len({p.pos.goal_group for p in pairs}),
                "repairs": repairs,
                "hard": hard,
                "hard_all": hard_all,
                "deviations": deviations,
                "review": review,
                "revision": revision,
            }
        ),
        extra={
            "stats": stats,
            "capped": capped,
            "failures": status,
            "revision": revision,
            "review_ids": [p.rec["id"] for p in review],
        },
    )
    report = cfg.paths.reports / f"{name}.md"
    if status:
        raise P5CheckError(
            f"P5 stopped, outputs not written. See {report}:\n  " + "\n  ".join(status)
        )
    out = None
    if not pilot_run:
        out = prepare_out(cfg.paths.out / OUT_FILE, force=force, cfg=cfg)
        write_parquet(out, samples)
        logger.info("Wrote %d samples -> %s", len(samples), out)
    return {"out": out, "report": report, "samples": len(samples), "revision": revision}
