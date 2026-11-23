"""P1. BIPIA: clean and split.

Outputs data/interim/bipia_docs.jsonl, data/interim/attacks.jsonl and reports/p1_counts.md.
The split unit is the distinct document: a document and all of its questions go to one split.
"""

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nltk.tokenize.punkt import PunktSentenceTokenizer

from dataset_builder.bipia_source import (
    OFFICIAL_SPLITS,
    bipia_root,
    describe_formats,
    fetch_bipia,
    load_attacks,
    load_contexts,
)
from dataset_builder.config import Config, load_config
from dataset_builder.io import prepare_out, rng, write_jsonl
from dataset_builder.reports import (
    CountMismatchError,
    check_expected,
    compute_counts,
    write_counts_report,
)

logger = logging.getLogger(__name__)

STEP = "p1"
DOCS_FILE = "bipia_docs.jsonl"
ATTACKS_FILE = "attacks.jsonl"
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}")
# A sentence end glued to the next sentence: letter/digit + [.!?] + Capitalised word.
_GLUED_SENTENCE = re.compile(r"(?<=[A-Za-z0-9][.!?])(?=[A-Z][a-z])")


class P1CheckError(RuntimeError):
    """A P1 check failed. The report has been written; outputs have not."""


# --------------------------------------------------------------------------- documents


def document_text(scenario: str, row: dict[str, Any]) -> str:
    """The untrusted content. Code contexts are lists of lines, joined as BIPIA does."""
    ctx = row["context"]
    return "\n".join(ctx) if isinstance(ctx, list) else ctx


def doc_id_for(scenario: str, text: str) -> str:
    return f"bipia-{scenario}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"


def _joined(v: Any) -> str | None:
    if v is None:
        return None
    return "\n".join(v) if isinstance(v, list) else v


def dedupe(scenario: str, official_split: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per distinct document text, in first-seen order. Questions are merged."""
    docs: dict[str, dict[str, Any]] = {}
    for row in rows:
        text = document_text(scenario, row)
        did = doc_id_for(scenario, text)
        doc = docs.get(did)
        if doc is None:
            doc = docs[did] = {
                "doc_id": did,
                "source": "bipia",
                "scenario": scenario,
                "source_split": official_split,
                "split": None,
                "document": text,
                "question": None,
                "questions": [],
                "ideal": None,
                "ideals": [],
                "code": None,
                "error": None,
                "context_url": row.get("context_url"),
                "first_row": row["_row"],
                "n_rows": 0,
                "middle_index": None,
                "middle_method": None,
            }
            if scenario == "code":
                # Code has no `question`: the user's request is their buggy code + traceback.
                doc["code"] = _joined(row.get("code"))
                doc["error"] = _joined(row.get("error"))
        doc["n_rows"] += 1
        if row.get("question") is not None:
            doc["questions"].append(row["question"])
        doc["ideals"].append(_joined(row.get("ideal")))
    for doc in docs.values():
        doc["question"] = doc["questions"][0] if doc["questions"] else None
        doc["ideal"] = doc["ideals"][0] if doc["ideals"] else None
    return list(docs.values())


@dataclass
class SplitResult:
    docs: list[dict[str, Any]]
    dropped_cross_split: list[str] = field(default_factory=list)
    # (doc_id, question) pairs from BIPIA's train file, dropped with their test documents.
    disregarded_questions: list[tuple[str, str | None]] = field(default_factory=list)
    moved_tables: list[str] = field(default_factory=list)
    dedupe_stats: list[dict[str, Any]] = field(default_factory=list)


def build_split(raw: dict[tuple[str, str], list[dict[str, Any]]], cfg: Config) -> SplitResult:
    """raw: {(scenario, official_split): rows}. Returns documents with `split` assigned.

    Test = official test documents, plus `tables_moved_to_test` train tables moved at random.
    A train-file document that is also in test is dropped from training, with its training
    questions. Validation is drawn from the remaining train documents; the rest is train.
    """
    b = cfg.bipia
    res = SplitResult(docs=[])
    for scenario in b.scenarios:
        by_split = {s: dedupe(scenario, s, raw[(scenario, s)]) for s in OFFICIAL_SPLITS}
        test_ids = {d["doc_id"] for d in by_split["test"]}
        train = [d for d in by_split["train"] if d["doc_id"] not in test_ids]
        dropped_docs = sorted(
            (d for d in by_split["train"] if d["doc_id"] in test_ids), key=lambda d: d["doc_id"]
        )
        res.dropped_cross_split += [d["doc_id"] for d in dropped_docs]
        res.disregarded_questions += [
            (d["doc_id"], q) for d in dropped_docs for q in (d["questions"] or [None] * d["n_rows"])
        ]
        res.dedupe_stats.append(
            {
                "scenario": scenario,
                "rows_train": len(raw[(scenario, "train")]),
                "rows_test": len(raw[(scenario, "test")]),
                "distinct_train": len(by_split["train"]),
                "distinct_test": len(by_split["test"]),
                "in_both": len(dropped_docs),
                "questions_disregarded": sum(d["n_rows"] for d in dropped_docs),
                "distinct_total": len(by_split["train"])
                + len(by_split["test"])
                - len(dropped_docs),
            }
        )
        for d in by_split["test"]:
            d["split"] = "test"
        train.sort(key=lambda d: d["doc_id"])  # draws never depend on file order
        if scenario == "table" and b.tables_moved_to_test:
            ids = [d["doc_id"] for d in train]
            if b.tables_moved_to_test > len(ids):
                raise P1CheckError(f"tables_moved_to_test {b.tables_moved_to_test} > {len(ids)}")
            moved = set(rng("p1:move_tables", cfg).sample(ids, b.tables_moved_to_test))
            for d in train:
                if d["doc_id"] in moved:
                    d["split"] = "test"
            res.moved_tables = sorted(moved)
        remaining = [d for d in train if d["split"] is None]
        n_val = b.validation_size.get(scenario, 0)
        if n_val > len(remaining):
            raise P1CheckError(f"{scenario}: validation_size {n_val} > {len(remaining)} train docs")
        val = set(
            rng(f"p1:validation:{scenario}", cfg).sample([d["doc_id"] for d in remaining], n_val)
        )
        for d in remaining:
            d["split"] = "validation" if d["doc_id"] in val else "train"
        res.docs += sorted(by_split["test"] + train, key=lambda d: (d["split"], d["doc_id"]))
    return res


def check_test_composition(
    docs: list[dict[str, Any]],
    raw: dict[tuple[str, str], list[dict[str, Any]]],
    scenarios: list[str],
    moved_tables: list[str],
    n_moved: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Test = every official test document (byte-identical, all rows kept) + the moved tables.

    Returns (failures, per-scenario table).
    """
    failures: list[str] = []
    table: list[dict[str, Any]] = []
    moved_set = set(moved_tables)
    if len(moved_set) != n_moved:
        failures.append(f"{len(moved_set)} tables moved to test, expected {n_moved}")
    for s in scenarios:
        rows = raw[(s, "test")]
        official = {doc_id_for(s, document_text(s, r)): document_text(s, r) for r in rows}
        test = [d for d in docs if d["scenario"] == s and d["split"] == "test"]
        ours_official = [d for d in test if d["doc_id"] in official]
        extra = [d for d in test if d["doc_id"] not in official]
        problems = []
        if missing := set(official) - {d["doc_id"] for d in ours_official}:
            problems.append(f"{len(missing)} official test documents missing from test")
        if changed := [
            d["doc_id"] for d in ours_official if official[d["doc_id"]] != d["document"]
        ]:
            problems.append(f"{len(changed)} official test texts changed")
        if bad := [d["doc_id"] for d in ours_official if d["source_split"] != "test"]:
            problems.append(f"{len(bad)} official test documents without source_split=test")
        rows_kept = sum(d["n_rows"] for d in ours_official)
        if rows_kept != len(rows):
            problems.append(f"rows kept {rows_kept} != official test rows {len(rows)}")
        not_moved = [
            d["doc_id"]
            for d in extra
            if d["doc_id"] not in moved_set or d["source_split"] != "train"
        ]
        if not_moved:
            problems.append(f"{len(not_moved)} test documents are neither official nor moved")
        failures += [f"{s} test composition: {p}" for p in problems]
        table.append(
            {
                "scenario": s,
                "official_test_docs": len(official),
                "official_test_rows": len(rows),
                "rows_kept": rows_kept,
                "moved_from_train": len(extra),
                "test_docs": len(test),
                "ok": not problems,
            }
        )
    return failures, table


# --------------------------------------------------------------------------- middle index


def _line_starts(text: str) -> list[int]:
    return [i + 1 for i, ch in enumerate(text) if ch == "\n"]


def _valid(text: str, cands: list[int]) -> list[int]:
    """Strictly inside the text, with real content on both sides."""
    return sorted({c for c in cands if 0 < c < len(text) and text[:c].strip() and text[c:].strip()})


def _sentence_starts(text: str) -> list[int]:
    return [s for s, _ in PunktSentenceTokenizer().span_tokenize(text)]


def _glued_sentence_starts(text: str) -> list[int]:
    """Sentence ends with no space after them, e.g. 'charged $3,620.00.They initiated'."""
    return [m.end() for m in _GLUED_SENTENCE.finditer(text)]


def _table_row_starts(text: str) -> list[int]:
    """Line starts from the 2nd data row on: the header (and its |---| separator line, if any)
    stays together with the first data row. BIPIA tables have no separator line."""
    lines = text.split("\n")
    starts = [0, *_line_starts(text)]
    sep = next((i for i, ln in enumerate(lines[:3]) if _TABLE_SEPARATOR.match(ln)), None)
    first_data = 1 if sep is None else sep + 1
    return starts[first_data + 1 :]


def middle_candidates(scenario: str, text: str) -> tuple[list[int], str]:
    """Candidate insertion offsets and the method that produced them.

    Primary: sentence starts (email), table rows (table), line starts (code). Fallbacks, in order:
    Punkt sentences, glued sentences, line starts, the word boundary nearest the centre.
    """
    primary = {
        "email": (_sentence_starts, "sentence"),
        "table": (_table_row_starts, "table_row"),
        "code": (_line_starts, "line"),
    }[scenario]
    chain = [
        primary,
        (_sentence_starts, "sentence_fallback"),
        (_glued_sentence_starts, "glued_sentence_fallback"),
        (_line_starts, "line_fallback"),
    ]
    for fn, method in chain:
        if cands := _valid(text, fn(text)):
            return cands, method
    words = _valid(text, [m.start() for m in re.finditer(r"(?<=\s)\S", text)])
    if words:
        centre = len(text) / 2
        return [min(words, key=lambda c: (abs(c - centre), c))], "word_fallback"
    return [], "none"


def pick_middle(doc: dict[str, Any], cfg: Config) -> tuple[int | None, str]:
    cands, method = middle_candidates(doc["scenario"], doc["document"])
    if not cands:
        return None, method
    return rng(f"p1:middle:{doc['doc_id']}", cfg).choice(cands), method


# --------------------------------------------------------------------------- attacks


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def build_attacks(raw: dict[tuple[str, str], dict[str, list[str]]]) -> list[dict[str, Any]]:
    out = []
    for (group, split), cats in raw.items():
        for category, texts in cats.items():
            for i, text in enumerate(texts):
                out.append(
                    {
                        "attack_id": f"{group}-{split}-{_slug(category)}-{i}",
                        "group": group,
                        "attack_group": category,
                        "attack_name": f"{category}-{i}",
                        "text": text,
                        "source_split": split,
                    }
                )
    return out


def check_attacks(
    attacks: list[dict[str, Any]], allowed_shared: list[str]
) -> tuple[list[str], list[str]]:
    """Returns (failures, deviations)."""
    failures: list[str] = []
    deviations: list[str] = []
    ids = Counter(a["attack_id"] for a in attacks)
    if dup := [k for k, n in ids.items() if n > 1]:
        failures.append(f"duplicate attack_id: {dup}")
    for group in sorted({a["group"] for a in attacks}):
        g = [a for a in attacks if a["group"] == group]
        by = {s: [a for a in g if a["source_split"] == s] for s in OFFICIAL_SPLITS}
        strings = {s: {a["text"].strip() for a in v} for s, v in by.items()}
        if shared := strings["train"] & strings["test"]:
            failures.append(f"{group}: {len(shared)} attack strings in both train and test")
        cats = {s: {a["attack_group"] for a in v} for s, v in by.items()}
        for name in sorted(cats["train"] & cats["test"]):
            if name in allowed_shared:
                deviations.append(
                    f"{group}: category name '{name}' is in both train and test. Its strings "
                    "differ (0 shared), so it is kept (allowed_shared_attack_groups). See "
                    "*Note for the team: shared attack-type name* for the impact and the "
                    "evaluation follow-up."
                )
            else:
                failures.append(f"{group}: attack type '{name}' is in both train and test")
    return failures, deviations


# --------------------------------------------------------------------------- checks + report


def check_docs(docs: list[dict[str, Any]]) -> list[str]:
    failures = []
    ids = Counter(d["doc_id"] for d in docs)
    if dup := [k for k, n in ids.items() if n > 1]:
        failures.append(f"{len(dup)} doc_id in more than one split/record")
    by_text: dict[str, set[str]] = {}
    for d in docs:
        by_text.setdefault(d["document"], set()).add(d["split"])
    if multi := [t for t, s in by_text.items() if len(s) > 1]:
        failures.append(f"{len(multi)} document texts in more than one split")
    test_ids = {d["doc_id"] for d in docs if d["split"] == "test"}
    if leaked := [d["doc_id"] for d in docs if d["split"] != "test" and d["doc_id"] in test_ids]:
        failures.append(f"{len(leaked)} test documents also in train/validation")
    if unassigned := [d["doc_id"] for d in docs if d["split"] is None]:
        failures.append(f"{len(unassigned)} documents without a split")
    bad = [
        d["doc_id"]
        for d in docs
        if d["middle_index"] is None or not 0 < d["middle_index"] < len(d["document"])
    ]
    if bad:
        failures.append(f"{len(bad)} documents without a valid middle_index: {bad[:5]}")
    return failures


def doc_counts(res: SplitResult, keys: tuple[str, ...]) -> dict[str, Any]:
    c = compute_counts(res.docs, keys)
    by_scenario = {
        s: {"split": dict(Counter(d["split"] for d in res.docs if d["scenario"] == s))}
        for s in sorted({d["scenario"] for d in res.docs})
    }
    return {
        "total": c["total"],
        "scenario": c["scenario"],
        "split": c["split"],
        "source_split": c["source_split"],
        "by_scenario": by_scenario,
        "cross_split_emails_dropped": len(res.dropped_cross_split),
        "train_questions_disregarded": len(res.disregarded_questions),
        "tables_moved_to_test": len(res.moved_tables),
    }


def attack_counts(attacks: list[dict[str, Any]]) -> dict[str, Any]:
    by_group: dict[str, Any] = {}
    for g in sorted({a["group"] for a in attacks}):
        ga = [a for a in attacks if a["group"] == g]
        by_group[g] = {
            "source_split": dict(Counter(a["source_split"] for a in ga)),
            "attack_groups": {
                s: len({a["attack_group"] for a in ga if a["source_split"] == s})
                for s in OFFICIAL_SPLITS
            },
        }
    return {"total": len(attacks), "by_group": by_group}


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def _split_n(dc: dict[str, Any], scenario: str, split: str) -> int:
    return dc["by_scenario"].get(scenario, {}).get("split", {}).get(split, 0)


def _rationale(dc: dict[str, Any], cfg: Config) -> str:
    v = cfg.bipia.validation_size
    t = {s: _split_n(dc, s, "test") for s in ("email", "table", "code")}
    return f"""**What follows BIPIA's split**

- Test documents (email and code): these are BIPIA's official test documents.
- Attacks: test documents only get BIPIA's test attacks. Train and validation documents only get
  the train attacks. The attack types in the two sets are different (checked in P1), so the model
  is tested on attack types it never saw.¹
- Train and validation: both come from BIPIA's official train documents.

**What is added on top**

- Split by document: BIPIA repeats the same document with many questions, and we insert many
  attacks into each document. If one document appeared in both train and test, the model could
  memorise it. So the split is by distinct document, and duplicates are merged.
- {dc["cross_split_emails_dropped"]} emails removed: they appear in both of BIPIA's official
  splits. They stay in test and are dropped from training, together with the
  {dc["train_questions_disregarded"]} questions written for training on them.
- Validation split: BIPIA has no validation split, so about 10% of each scenario's distinct
  documents are held out from the remaining train documents ({v.get("email", 0)} email,
  {v.get("table", 0)} table, {v.get("code", 0)} code).

**One deviation from BIPIA's split: tables**

- Tables are {dc["scenario"].get("table", 0)} of the {dc["total"]} documents, but BIPIA's official
  test set has few of them.
- {dc["tables_moved_to_test"]} table documents are moved at random from BIPIA's train set into
  test, so the test set has {t["table"]} tables (about 30% of distinct tables).
- The test set then has {dc["split"].get("test", 0)} documents in total ({t["email"]} email,
  {t["table"]} table, {t["code"]} code). Every official test document is kept unchanged; see
  *Test composition*.

¹ One category *name*, "Language Translation", is in both attack files. Its strings differ
(train: translate a given phrase; test: answer in another language), and no attack string is
shared. See *Deviations from the brief*.
"""


_EXAMPLE = """The split unit is the document, never the row. A document goes to exactly one
split and takes all of its questions with it.

| file | row | document | question |
|---|---|---|---|
| table/train.jsonl | 1 | T1 | q1 |
| table/train.jsonl | 2 | T1 | q2 |
| table/train.jsonl | 3 | T2 | q3 |
| table/test.jsonl | 1 | T3 | q4 |
| email/train.jsonl | 1 | E1 | q5 |
| email/test.jsonl | 1 | E1 | q6 |

1. Deduplicate within each file: `T1` → questions [q1, q2]; `T2` → [q3]; `T3` → [q4];
   `E1` (train) → [q5]; `E1` (test) → [q6].
2. Document in both files: the train copy of `E1` is dropped and its training question q5 is
   disregarded. `E1` stays in test, with q6.
3. Split per document: `T3` and `E1` go to test (official test). If `T2` is drawn among the
   moved tables it goes to test with `source_split=train`. If `T1` is drawn for validation, it
   goes there with both q1 and q2. The rest is train.
"""


def _shared_name_note(attacks: list[dict[str, Any]], allowed: list[str]) -> str:
    """Explain each allowed attack-type name that is in both train and test (for the team)."""
    parts: list[str] = []
    for name in allowed:
        by = {
            s: [a for a in attacks if a["attack_group"] == name and a["source_split"] == s]
            for s in OFFICIAL_SPLITS
        }
        if not (by["train"] and by["test"]):
            continue
        group = by["test"][0]["group"]
        n_test_group = sum(
            1 for a in attacks if a["group"] == group and a["source_split"] == "test"
        )
        shared = {a["text"].strip() for a in by["train"]} & {a["text"].strip() for a in by["test"]}
        rows = [
            [f"String {i + 1}", tr["text"].replace("|", "\\|"), te["text"].replace("|", "\\|")]
            for i, (tr, te) in enumerate(zip(by["train"], by["test"], strict=False))
        ]
        parts.append(
            f"""The brief asks that train and test attack *types* be disjoint, so that the test set
measures generalisation to kinds of injection the model never saw. In BIPIA, one type
name is in both the train and the test {group} attack files: **"{name}"**. The other names are
all different.

{_table(["", f'Train "{name}"', f'Test "{name}"'], rows)}

- **By name:** not disjoint. A literal check of the brief fails on this one type.
- **By content:** disjoint. {len(shared)} of the {len(by["train"]) + len(by["test"])} strings
  are shared, and they ask for different things. The train strings ask for an *extra task*
  (translate a given phrase); BIPIA files them with the other off-task requests. The test strings
  ask for a *behaviour change* (answer in another language); BIPIA files them with the
  encoding/obfuscation attacks. BIPIA seems to have reused one label for two different attacks.
- **Possible effect:** both are about languages, so training on the train strings may make the
  test strings slightly easier than a truly unseen type. This affects {len(by["test"])} of the
  {n_test_group} {group} test attacks ({len(by["test"]) / n_test_group:.0%}), on email and table
  test samples only (code documents get code attacks).

**Decision:** keep all attacks, so the counts match the brief and BIPIA. Record the shared name as
a deviation (here and in *Deviations from the brief*).

**Follow-up for evaluation:** every sample carries `attack_group` (from P2 onwards). Report test
metrics **per `attack_group`**, and the headline metric **with and without "{name}"**. That makes
any advantage from this overlap visible.
"""
        )
    return "\n".join(parts)


def _sections(
    cfg: Config,
    sha: str,
    root: Path,
    res: SplitResult,
    dc: dict[str, Any],
    ac: dict[str, Any],
    attacks: list[dict[str, Any]],
    comp: list[dict[str, Any]],
    status: list[str],
    deviations: list[str],
) -> list[tuple[str, str]]:
    b = cfg.bipia
    mm = Counter((d["scenario"], d["middle_method"]) for d in res.docs)
    cats = {
        (g, s): sorted(
            {a["attack_group"] for a in attacks if a["group"] == g and a["source_split"] == s}
        )
        for g in ("text", "code")
        for s in OFFICIAL_SPLITS
    }
    disregarded = Counter(i for i, _ in res.disregarded_questions)
    comp_tbl = _table(
        [
            "scenario",
            "official test docs",
            "official test rows",
            "rows kept",
            "moved from train",
            "test docs",
            "ok",
        ],
        [
            [
                c["scenario"],
                c["official_test_docs"],
                c["official_test_rows"],
                c["rows_kept"],
                c["moved_from_train"],
                c["test_docs"],
                "yes" if c["ok"] else "**NO**",
            ]
            for c in comp
        ],
    )
    dedupe = _table(
        [
            "scenario",
            "rows train",
            "rows test",
            "distinct train",
            "distinct test",
            "in both",
            "train questions disregarded",
            "distinct total",
        ],
        [
            [
                s["scenario"],
                s["rows_train"],
                s["rows_test"],
                s["distinct_train"],
                s["distinct_test"],
                s["in_both"],
                s["questions_disregarded"],
                s["distinct_total"],
            ]
            for s in res.dedupe_stats
        ],
    )
    split_tbl = _table(
        ["scenario", "train", "validation", "test", "of which moved from train"],
        [
            [
                s,
                *(_split_n(dc, s, sp) for sp in ("train", "validation", "test")),
                len(res.moved_tables) if s == "table" else 0,
            ]
            for s in b.scenarios
        ],
    )
    attacks_tbl = _table(
        ["group", "source_split", "attacks", "types"],
        [
            [g, s, ac["by_group"][g]["source_split"].get(s, 0), len(cats[(g, s)])]
            for g in ("text", "code")
            for s in OFFICIAL_SPLITS
        ],
    )
    passed = [
        f"- PASSED: test = official test documents (unchanged, all rows kept) + "
        f"{len(res.moved_tables)} moved tables",
        "- PASSED: no test document in train or validation",
        "- PASSED: every document is in exactly one split; no text in two splits",
        "- PASSED: every middle_index is a boundary strictly inside its document",
        f"- PASSED: {len(res.dropped_cross_split)} cross-split emails dropped from train "
        f"({len(res.disregarded_questions)} training questions disregarded)",
        "- PASSED: no attack string in both train and test; attack types disjoint "
        "(except the allowed name below)",
        "- PASSED: all counts match `expected_counts` (p1_docs, p1_attacks)",
    ]
    return [
        (
            "Source",
            f"- Repository: {b.repo_url}\n- Commit: `{sha}`\n- Scenarios: {', '.join(b.scenarios)}"
            " (web QA and summarisation excluded: their contexts cannot be redistributed)",
        ),
        ("File formats found", describe_formats(root, b.scenarios)),
        (
            "Deduplication (exact document text)",
            dedupe
            + "\n\nDocuments in both official splits. Their train copies were dropped, with the "
            "training questions shown:\n\n"
            + "\n".join(f"- `{i}` ({disregarded[i]} question(s))" for i in res.dropped_cross_split),
        ),
        ("Split by scenario", split_tbl),
        (
            "Test composition",
            comp_tbl
            + "\n\n*ok* = every official test document is in test with its text unchanged and all "
            "its rows kept as questions; every other test document is a moved train table.",
        ),
        (
            "middle_index method",
            _table(
                ["scenario", "method", "documents"], [[s, m, n] for (s, m), n in sorted(mm.items())]
            ),
        ),
        (
            "Attacks",
            attacks_tbl
            + "\n\n"
            + "\n".join(f"- **{g} / {s}**: {', '.join(c)}" for (g, s), c in cats.items()),
        ),
        *(
            [("Note for the team: shared attack-type name", note)]
            if (note := _shared_name_note(attacks, b.allowed_shared_attack_groups))
            else []
        ),
        ("Checks", "\n".join([f"- FAILED: {f}" for f in status] or passed)),
        ("Deviations from the brief", "\n".join(f"- {d}" for d in deviations)),
    ]


def run(cfg: Config | None = None, force: bool = False) -> dict[str, Any]:
    cfg = cfg or load_config()
    sha = fetch_bipia(cfg)
    root = bipia_root(cfg)
    b = cfg.bipia

    raw = {(s, sp): load_contexts(root, s, sp) for s in b.scenarios for sp in OFFICIAL_SPLITS}
    res = build_split(raw, cfg)
    for d in res.docs:
        d["middle_index"], d["middle_method"] = pick_middle(d, cfg)
    attacks = build_attacks(load_attacks(root))

    failures = check_docs(res.docs)
    comp_fail, comp = check_test_composition(
        res.docs, raw, b.scenarios, res.moved_tables, b.tables_moved_to_test
    )
    failures += comp_fail
    if len(res.dropped_cross_split) != b.expected_cross_split_emails:
        failures.append(
            f"{len(res.dropped_cross_split)} documents in both official splits, "
            f"expected {b.expected_cross_split_emails}"
        )
    att_fail, att_deviations = check_attacks(attacks, b.allowed_shared_attack_groups)
    failures += att_fail
    deviations = [
        *att_deviations,
        "Email questions keep BIPIA's 'Q: ' prefix. They are stored as is; the step that builds"
        " the query decides whether to strip it.",
        "The code scenario has no `question` field: the user's request is `code` + `error`.",
    ]

    keys = ("scenario", "split", "source_split")
    dc, ac = doc_counts(res, keys), attack_counts(attacks)
    count_errors: list[str] = []
    for step, actual in (("p1_docs", dc), ("p1_attacks", ac)):
        try:
            check_expected(step, actual, cfg=cfg)
        except CountMismatchError as e:  # report every mismatch, then stop
            count_errors.append(str(e))
    status = failures + count_errors

    write_counts_report(
        STEP,
        res.docs,
        keys=keys,
        cfg=cfg,
        preamble=[("How the split is built", _rationale(dc, cfg)), ("Example", _EXAMPLE)],
        sections=_sections(cfg, sha, root, res, dc, ac, attacks, comp, status, deviations),
        extra={
            "repo_commit": sha,
            "p1_docs": dc,
            "p1_attacks": ac,
            "test_composition": comp,
            "moved_tables": res.moved_tables,
            "failures": status,
            "deviations": deviations,
        },
    )
    report = cfg.paths.reports / f"{STEP}_counts.md"
    if status:
        raise P1CheckError(
            f"P1 stopped, outputs not written. See {report}:\n  " + "\n  ".join(status)
        )

    docs_path = prepare_out(cfg.paths.interim / DOCS_FILE, force=force, cfg=cfg)
    attacks_path = prepare_out(cfg.paths.interim / ATTACKS_FILE, force=force, cfg=cfg)
    write_jsonl(docs_path, res.docs)
    write_jsonl(attacks_path, attacks)
    logger.info("Wrote %d docs -> %s", len(res.docs), docs_path)
    logger.info("Wrote %d attacks -> %s", len(attacks), attacks_path)
    return {"docs": Path(docs_path), "attacks": Path(attacks_path), "report": report, "commit": sha}
