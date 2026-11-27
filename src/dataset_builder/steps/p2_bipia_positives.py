"""P2. BIPIA: attacked samples (label 1).

Outputs data/interim/bipia_positives_{train,validation,test}.parquet and reports/p2/p2_counts.md.

- Test: every test document x every test attack of its scenario's group, one position each,
  balanced with a shuffled Latin square.
- Train / validation: `n_per_doc` (attack, position) combinations per document. Attacks come from
  a shuffled cycle (reshuffled each time the pool is used up), positions from a continuing
  start -> middle -> end cycle.
"""

import hashlib
import logging
import re
from collections import Counter, deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dataset_builder.config import Config, load_config
from dataset_builder.insert import POSITIONS, InsertionError, insert, remove
from dataset_builder.io import prepare_out, read_jsonl, rng, write_parquet
from dataset_builder.query import candidate_questions, query, user_request
from dataset_builder.reports import (
    CountMismatchError,
    check_expected,
    report_path,
    write_counts_report,
)
from dataset_builder.schema import COLUMNS, Sample
from dataset_builder.steps.p1_bipia_split import ATTACKS_FILE, DOCS_FILE

logger = logging.getLogger(__name__)

STEP = "p2"
SPLITS = ("train", "validation", "test")
POOL_GROUP = {"email": "text", "table": "text", "code": "code"}
ATTACK_SPLIT = {"train": "train", "validation": "train", "test": "test"}
INTERIM_COLUMNS = ("doc_id", "attack_id")  # kept for P4 and the checks; dropped in P10
_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)

Combo = tuple[dict[str, Any], dict[str, Any], str]  # (doc, attack, position)


class P2CheckError(RuntimeError):
    """A P2 check failed. The report has been written; outputs have not."""


def output_path(cfg: Config, split: str) -> Path:
    return cfg.paths.interim / f"bipia_positives_{split}.parquet"


# --------------------------------------------------------------------------- sampling


def attack_pool(attacks: list[dict[str, Any]], scenario: str, split: str) -> list[dict[str, Any]]:
    """Attacks allowed for (scenario, split): right group and right source split."""
    group, src = POOL_GROUP[scenario], ATTACK_SPLIT[split]
    pool = [a for a in attacks if a["group"] == group and a["source_split"] == src]
    return sorted(pool, key=lambda a: a["attack_id"])


def test_grid(
    docs: list[dict[str, Any]], pool: list[dict[str, Any]], scenario: str, cfg: Config
) -> Iterator[Combo]:
    """Every doc x every attack once. Position = shuffled Latin square, so positions are balanced
    per scenario, per document and per attack."""
    r = rng(f"p2:test:{scenario}", cfg)
    ds = sorted(docs, key=lambda d: d["doc_id"])
    ps = list(pool)
    r.shuffle(ds)
    r.shuffle(ps)
    offset = r.randrange(len(POSITIONS))
    for i, doc in enumerate(ds):
        for j, attack in enumerate(ps):
            yield doc, attack, POSITIONS[(i + j + offset) % len(POSITIONS)]


def sampled_combos(
    docs: list[dict[str, Any]],
    pool: list[dict[str, Any]],
    n_per_doc: int,
    split: str,
    scenario: str,
    cfg: Config,
) -> Iterator[Combo]:
    """`n_per_doc` distinct attacks per document from a shuffled cycle; positions cycle."""
    if n_per_doc > len(pool):
        raise P2CheckError(f"n_per_doc {n_per_doc} > {len(pool)} attacks for {scenario}")
    r = rng(f"p2:{split}:{scenario}", cfg)
    ds = sorted(docs, key=lambda d: d["doc_id"])
    r.shuffle(ds)
    queue: deque[dict[str, Any]] = deque()
    pos_i = r.randrange(len(POSITIONS))
    for doc in ds:
        chosen: list[dict[str, Any]] = []
        used: set[str] = set()
        skipped: list[dict[str, Any]] = []
        while len(chosen) < n_per_doc:
            if not queue:  # start a new, freshly shuffled cycle
                cycle = list(pool)
                r.shuffle(cycle)
                queue.extend(cycle)
            a = queue.popleft()
            if a["attack_id"] in used:  # only possible across a cycle boundary
                skipped.append(a)
                continue
            used.add(a["attack_id"])
            chosen.append(a)
        queue.extendleft(reversed(skipped))  # skipped attacks are used next
        for a in chosen:
            yield doc, a, POSITIONS[pos_i % len(POSITIONS)]
            pos_i += 1


def code_blocks(text: str) -> list[str]:
    return _CODE_BLOCK.findall(text) or [text]


def make_sample(
    doc: dict[str, Any], attack: dict[str, Any], position: str, q: str
) -> dict[str, Any]:
    new_text, idx = insert(doc["document"], attack["text"], position, doc["middle_index"])
    h = hashlib.sha1(f"{doc['doc_id']}|{attack['attack_id']}|{position}".encode()).hexdigest()[:16]
    sample = Sample(
        sample_id=f"bipia-pos-{h}",
        parent_id=f"pair-{h}",
        source="bipia",
        split=doc["split"],
        source_split=doc["source_split"],
        scenario=doc["scenario"],
        query=q,
        document=new_text,
        label=1,
        answer="yes",
        label_provenance="construction",
        generation_method="sampled_insertion",
        attack_group=attack["attack_group"],
        attack_name=attack["attack_name"],
        position=position,
        insert_index=idx,
        injection_span=attack["text"],
        protected_tokens=code_blocks(attack["text"]) if attack["group"] == "code" else None,
    )
    return sample.model_dump() | {"doc_id": doc["doc_id"], "attack_id": attack["attack_id"]}


def build_positives(
    docs: list[dict[str, Any]], attacks: list[dict[str, Any]], n_per_doc: int, cfg: Config
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for split in SPLITS:
        for scenario in cfg.bipia.scenarios:
            ds = [d for d in docs if d["split"] == split and d["scenario"] == scenario]
            if not ds:
                continue
            pool = attack_pool(attacks, scenario, split)
            combos = (
                test_grid(ds, pool, scenario, cfg)
                if split == "test"
                else sampled_combos(ds, pool, n_per_doc, split, scenario, cfg)
            )
            rows += [
                make_sample(d, a, p, query(d, cfg, choose_question(d, a, p, cfg)))
                for d, a, p in combos
            ]
    return rows


def choose_question(
    doc: dict[str, Any], attack: dict[str, Any], position: str, cfg: Config
) -> str | None:
    """Test: the document's first question (fixed counts, comparable). Train/validation: one
    question drawn at random from the document's list, seeded per sample. Code: None (the query
    is built from the traceback)."""
    candidates = candidate_questions(doc)
    if doc["split"] == "test" or len(candidates) == 1:
        return candidates[0]
    key = f"p2:question:{doc['doc_id']}|{attack['attack_id']}|{position}"
    return rng(key, cfg).choice(candidates)


# --------------------------------------------------------------------------- checks


def _spread(counter: Counter) -> tuple[int, int]:
    return (min(counter.values()), max(counter.values())) if counter else (0, 0)


def check_rows(
    rows: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    attacks: list[dict[str, Any]],
    n_per_doc: int,
) -> tuple[list[str], dict[str, Any]]:
    """Returns (failures, balance stats)."""
    failures: list[str] = []
    doc_by_id = {d["doc_id"]: d for d in docs}
    att_by_id = {a["attack_id"]: a for a in attacks}

    not_full, not_clean = [], []
    for r in rows:
        span, i = r["injection_span"], r["insert_index"]
        if r["document"][i : i + len(span)] != span:
            not_full.append(r["sample_id"])
            continue
        try:
            clean = remove(r["document"], span, r["position"], i)
        except InsertionError:
            clean = None
        if clean != doc_by_id[r["doc_id"]]["document"]:
            not_clean.append(r["sample_id"])
    if not_full:
        failures.append(f"{len(not_full)} samples do not contain their full attack")
    if not_clean:
        failures.append(f"{len(not_clean)} samples do not give back the clean document")

    wrong_pool = [
        r["sample_id"]
        for r in rows
        if att_by_id[r["attack_id"]]["source_split"] != ATTACK_SPLIT[r["split"]]
        or att_by_id[r["attack_id"]]["group"] != POOL_GROUP[r["scenario"]]
    ]
    if wrong_pool:
        failures.append(f"{len(wrong_pool)} samples use an attack from the wrong split or group")
    if moved := [r["sample_id"] for r in rows if r["split"] != doc_by_id[r["doc_id"]]["split"]]:
        failures.append(f"{len(moved)} samples are not in their document's P1 split")

    # Test: each (doc, attack) exactly once.
    pairs = Counter((r["doc_id"], r["attack_id"]) for r in rows if r["split"] == "test")
    expected_pairs = {
        (d["doc_id"], a["attack_id"])
        for d in docs
        if d["split"] == "test"
        for a in attack_pool(attacks, d["scenario"], "test")
    }
    if set(pairs) != expected_pairs or any(n != 1 for n in pairs.values()):
        failures.append(
            f"test grid incomplete or repeated: {len(expected_pairs - set(pairs))} missing, "
            f"{sum(n > 1 for n in pairs.values())} repeated"
        )

    # Train / validation: n_per_doc distinct attacks per document.
    per_doc: dict[str, list[str]] = {}
    for r in rows:
        if r["split"] != "test":
            per_doc.setdefault(r["doc_id"], []).append(r["attack_id"])
    sampled_docs = [d["doc_id"] for d in docs if d["split"] != "test"]
    if bad := [i for i in sampled_docs if len(per_doc.get(i, [])) != n_per_doc]:
        failures.append(f"{len(bad)} train/validation documents without {n_per_doc} samples")
    if rep := [i for i, a in per_doc.items() if len(set(a)) != len(a)]:
        failures.append(f"{len(rep)} train/validation documents repeat an attack")

    # Balance per (split, scenario).
    stats: dict[str, Any] = {}
    for split in SPLITS:
        for scenario in sorted({r["scenario"] for r in rows}):
            sub = [r for r in rows if r["split"] == split and r["scenario"] == scenario]
            if not sub:
                continue
            pos = Counter(r["position"] for r in sub)
            names = Counter(r["attack_name"] for r in sub)
            groups = Counter(r["attack_group"] for r in sub)
            cells = Counter((r["attack_name"], r["position"]) for r in sub)
            key = f"{split}/{scenario}"
            stats[key] = {
                "rows": len(sub),
                "position": dict(sorted(pos.items())),
                "attack_name": {
                    "n": len(names),
                    "min": _spread(names)[0],
                    "max": _spread(names)[1],
                },
                "attack_group": {
                    "n": len(groups),
                    "min": _spread(groups)[0],
                    "max": _spread(groups)[1],
                },
                "attack_x_position": {"min": _spread(cells)[0], "max": _spread(cells)[1]},
            }
            lo, hi = _spread(pos)
            if hi - lo > 1 or len(pos) != len(POSITIONS):
                failures.append(f"{key}: positions not balanced {dict(pos)}")
            tol = 1 if split == "test" else 2
            lo, hi = _spread(names)
            if hi - lo > tol:
                failures.append(f"{key}: attack counts spread {lo}..{hi} > {tol}")

    # Queries: test = first question; train/validation = one of the document's questions.
    not_first, not_listed = [], []
    for r in rows:
        doc = doc_by_id[r["doc_id"]]
        if r["split"] == "test":
            if r["query"] != query(doc):
                not_first.append(r["sample_id"])
        elif r["query"] not in {query(doc, question=q) for q in candidate_questions(doc)}:
            not_listed.append(r["sample_id"])
    if not_first:
        failures.append(f"{len(not_first)} test samples do not use the first question")
    if not_listed:
        failures.append(
            f"{len(not_listed)} train/validation samples use a question not in the list"
        )

    for col in ("sample_id", "parent_id"):
        if len({r[col] for r in rows}) != len(rows):
            failures.append(f"{col} is not unique")
    return failures, stats


def _n_multi(docs: list[dict[str, Any]], split: str) -> int:
    """Documents of `split` with more than one question."""
    return sum(len(candidate_questions(d)) > 1 for d in docs if d["split"] == split)


def question_stats(rows: list[dict[str, Any]], docs: list[dict[str, Any]]) -> list[list[Any]]:
    """Per (split, scenario): documents, documents with >1 question, distinct queries used."""
    out = []
    for split in SPLITS:
        for s in ("email", "table", "code"):
            ds = [d for d in docs if d["split"] == split and d["scenario"] == s]
            if not ds:
                continue
            multi = [d for d in ds if len(candidate_questions(d)) > 1]
            multi_ids = {d["doc_id"] for d in multi}
            available = sum(len(candidate_questions(d)) for d in multi)
            used = len({(r["doc_id"], r["query"]) for r in rows if r["doc_id"] in multi_ids})
            out.append([split, s, len(ds), len(multi), available, used])
    return out


def counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_split = {}
    for split in SPLITS:
        sub = [r for r in rows if r["split"] == split]
        by_split[split] = {
            "total": len(sub),
            "scenario": dict(sorted(Counter(r["scenario"] for r in sub).items())),
        }
    return {
        "total": len(rows),
        "label": {str(k): v for k, v in Counter(r["label"] for r in rows).items()},
        "by_split": by_split,
    }


# --------------------------------------------------------------------------- report


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def _esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "⏎")


def _window(r: dict[str, Any], width: int = 120) -> str:
    """The text around the insertion, with the attack marked."""
    i, span, doc = r["insert_index"], r["injection_span"], r["document"]
    before = doc[max(0, i - width) : i]
    after = doc[i + len(span) : i + len(span) + width]
    short = span if len(span) <= 80 else span[:77] + "..."
    return f"…{before}⟦ATTACK: {short}⟧{after}…"


def _sections(
    rows: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    stats: dict[str, Any],
    n_per_doc: int,
    status: list[str],
) -> list[tuple[str, str]]:
    c = counts(rows)
    n_docs = Counter((d["split"], d["scenario"]) for d in docs)
    count_rows = []
    for split in SPLITS:
        for s in ("email", "table", "code"):
            n = c["by_split"][split]["scenario"].get(s, 0)
            how = (
                f"{n_docs[(split, s)]} docs x {n // max(n_docs[(split, s)], 1)} attacks"
                if split == "test"
                else f"{n_docs[(split, s)]} docs x {n_per_doc} (derived)"
            )
            count_rows.append([split, s, n, how])
    example_docs = {}
    for d in docs:
        example_docs.setdefault(d["scenario"], d)
    examples = []
    for s in ("email", "table", "code"):
        r = next(r for r in rows if r["scenario"] == s and r["split"] == "test")
        examples.append(
            f"**{s}** (`{r['sample_id']}`, {r['attack_name']}, position={r['position']}, "
            f"insert_index={r['insert_index']})\n\n- query: {r['query']}\n\n```text\n"
            f"{_window(r)}\n```"
        )
    insertion_demo = "\n".join(
        f"| {p} | `{_esc(insert('Line one.' + chr(10) + 'Line two.', 'ATTACK', p, 10)[0])}` |"
        for p in POSITIONS
    )
    passed = [
        "- PASSED: every sample contains its full attack at `insert_index`",
        "- PASSED: removing the inserted text gives back the clean P1 document exactly",
        "- PASSED: test documents only get test attacks, train/validation only train attacks; "
        "text attacks for email/table, code attacks for code",
        "- PASSED: test = every test document x every test attack of its scenario, once",
        f"- PASSED: train/validation = {n_per_doc} distinct attacks per document",
        "- PASSED: positions balanced within 1 per (split, scenario); attack counts within "
        "tolerance (1 for test, 2 for train/validation)",
        "- PASSED: test samples all use the first question; train/validation samples use a "
        "question from the document's list",
        "- PASSED: `sample_id` and `parent_id` unique; counts match `expected_counts.p2_positives`",
    ]
    return [
        (
            "Counts per split and scenario",
            _table(["split", "scenario", "samples", "how"], count_rows),
        ),
        (
            "Insertion rule (`dataset_builder.insert`)",
            "Pieces are joined with one newline, as in BIPIA. `insert_index` is where the "
            "insertion starts. Example on `Line one.⏎Line two.` with middle_index=10 "
            "(⏎ = newline):\n\n| position | result |\n|---|---|\n" + insertion_demo,
        ),
        (
            "Queries",
            _table(
                ["scenario", "user request (from doc)"],
                [[s, _esc(user_request(example_docs[s]))] for s in ("email", "table", "code")],
            )
            + "\n\nThe query is the user request alone (no wrapper), and it is stored in "
            "each sample (P4 reuses it exactly for the negative). Email/table: a question without "
            "its `Q:` prefix. Code: `Fix the bug in my code. Error: <last line of the traceback>`.",
        ),
        (
            "Questions",
            "Test samples all use the document's **first** question (fixed counts). Train and "
            "validation samples use **one question drawn at random** (seeded per sample) from the "
            "document's list kept in P1. Code documents have no questions.\n\n"
            + _table(
                [
                    "split",
                    "scenario",
                    "documents",
                    "documents with >1 question",
                    "questions available (those docs)",
                    "questions used (those docs)",
                ],
                question_stats(rows, docs),
            )
            + "\n\n**Train and validation documents with more than one question: "
            f"{_n_multi(docs, 'train') + _n_multi(docs, 'validation')}** "
            f"(train {_n_multi(docs, 'train')}, validation {_n_multi(docs, 'validation')}).",
        ),
        (
            "Balance per split and scenario",
            _table(
                [
                    "split / scenario",
                    "rows",
                    "start",
                    "middle",
                    "end",
                    "attacks used",
                    "per attack min-max",
                    "attack types",
                    "per type min-max",
                    "attack x position min-max",
                ],
                [
                    [
                        k,
                        v["rows"],
                        v["position"].get("start", 0),
                        v["position"].get("middle", 0),
                        v["position"].get("end", 0),
                        v["attack_name"]["n"],
                        f"{v['attack_name']['min']}-{v['attack_name']['max']}",
                        v["attack_group"]["n"],
                        f"{v['attack_group']['min']}-{v['attack_group']['max']}",
                        f"{v['attack_x_position']['min']}-{v['attack_x_position']['max']}",
                    ]
                    for k, v in stats.items()
                ],
            ),
        ),
        ("Example samples (test)", "\n\n".join(examples)),
        ("Checks", "\n".join([f"- FAILED: {f}" for f in status] or passed)),
        (
            "Deviations from the brief",
            "\n".join(
                [
                    "- Code query: the error line is the last line of the document's `error` "
                    "field (BIPIA's traceback). The brief says to find it in the code context, "
                    f"but only {_in_context(docs)} of the "
                    f"{sum(d['scenario'] == 'code' for d in docs)} Stack Overflow contexts "
                    "contain it.",
                    f"- Middle insertion at a line boundary leaves a blank line before the attack "
                    f"({sum(d['document'][d['middle_index'] - 1] == chr(10) for d in docs)} of "
                    f"{len(docs)} documents), as in BIPIA's `insert_middle`. P4 uses the same "
                    "function, so both classes have it and it is not a label shortcut.",
                    '- Step 6, "use the error message that goes with the chosen question": code '
                    "documents have no questions and one traceback each, so there is only one "
                    "error message per code document.",
                    "- Train/validation counts are not in the brief: they are P1 documents x "
                    f"n_per_doc ({n_per_doc}).",
                    "- Interim-only columns `doc_id` and `attack_id` are kept for P4 and the "
                    "checks; they are dropped in P10.",
                    '- Reminder (P1): "Language Translation" is a type name in both train and test '
                    "attacks. Report test metrics per `attack_group`.",
                ]
            ),
        ),
    ]


def _in_context(docs: list[dict[str, Any]]) -> int:
    from dataset_builder.query import error_line

    return sum(error_line(d) in d["document"] for d in docs if d["scenario"] == "code")


# --------------------------------------------------------------------------- run


def run(cfg: Config | None = None, force: bool = False, n_per_doc: int | None = None) -> dict:
    cfg = cfg or load_config()
    n_per_doc = cfg.bipia.n_per_doc if n_per_doc is None else n_per_doc
    docs = list(read_jsonl(cfg.paths.interim / DOCS_FILE))
    attacks = list(read_jsonl(cfg.paths.interim / ATTACKS_FILE))

    rows = build_positives(docs, attacks, n_per_doc, cfg)
    failures, stats = check_rows(rows, docs, attacks, n_per_doc)
    try:
        check_expected("p2_positives", counts(rows), cfg=cfg)
    except CountMismatchError as e:
        failures.append(str(e))

    write_counts_report(
        STEP,
        rows,
        cfg=cfg,
        sections=_sections(rows, docs, stats, n_per_doc, failures),
        extra={
            "p2_positives": counts(rows),
            "balance": stats,
            "n_per_doc": n_per_doc,
            "failures": failures,
        },
    )
    report = report_path(cfg, f"{STEP}_counts.md")
    if failures:
        raise P2CheckError(
            f"P2 stopped, outputs not written. See {report}:\n  " + "\n  ".join(failures)
        )

    paths = {}
    for split in SPLITS:
        path = prepare_out(output_path(cfg, split), force=force, cfg=cfg)
        sub = [r for r in rows if r["split"] == split]
        n = write_parquet(
            path,
            [Sample(**{c: r[c] for c in COLUMNS}) for r in sub],
            {c: [r[c] for r in sub] for c in INTERIM_COLUMNS},
        )
        logger.info("Wrote %d %s positives -> %s", n, split, path)
        paths[split] = path
    return {"paths": paths, "report": report, "rows": len(rows)}
