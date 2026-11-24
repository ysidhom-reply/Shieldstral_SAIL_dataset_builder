"""P4. BIPIA: assemble the negatives (label 0) and write the `bipia` config.

For every P2 positive, a P3 pool text for the same document and position replaces the attack:
same document, position, insert_index, query and instruct, so within a pair only the inserted
text differs. Output: data/out/bipia/{train,validation,test}.parquet (contract columns only)
and reports/p4/p4_composition.md.
"""

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from dataset_builder.benign_filters import AttackIndex, group_of, normalise
from dataset_builder.config import Config, load_config
from dataset_builder.insert import InsertionError, insert, remove
from dataset_builder.io import prepare_out, read_jsonl, read_parquet, write_jsonl, write_parquet
from dataset_builder.reports import (
    CountMismatchError,
    check_expected,
    report_path,
    write_counts_report,
)
from dataset_builder.schema import COLUMNS, Sample
from dataset_builder.steps.p1_bipia_split import ATTACKS_FILE, DOCS_FILE

logger = logging.getLogger(__name__)

STEP = "p4"
REPORT_NAME = "p4_composition"
SPLITS = ("train", "validation", "test")
SCENARIOS = ("email", "table", "code")
POOL_FILE = "benign_pool.jsonl"
PAIRS_FILE = "p4_pairs.jsonl"


class P4CheckError(RuntimeError):
    """A P4 check failed. The report has been written; data/out has not."""


def out_path(cfg: Config, split: str) -> Path:
    return cfg.paths.out / "bipia" / f"{split}.parquet"


def load_positives(cfg: Config) -> list[dict[str, Any]]:
    rows = []
    for split in SPLITS:
        rows += read_parquet(cfg.paths.interim / f"bipia_positives_{split}.parquet")
    return sorted(
        rows, key=lambda r: (SPLITS.index(r["split"]), r["doc_id"], r["position"], r["sample_id"])
    )


# --------------------------------------------------------------------------- picking


_WORD = re.compile(r"[a-z]+")


def avoid_score(text: str, avoid: frozenset[str]) -> int:
    """Number of distinct over-represented benign words in a text (P7 v0 diagnostics)."""
    return len(set(_WORD.findall(text.lower())) & avoid) if avoid else 0


def pick(
    attack_len: int,
    candidates: list[dict[str, Any]],
    uses: Counter,
    window: float,
    avoid: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], bool]:
    """(pool text, length_matched).

    Order: least used first, then a text within the length window, then the fewest
    over-represented benign words (`p4.avoid_words`), then the closest length, then the id.
    Why least used first (team decision): with the window first, the few texts close to a long
    attack were picked again and again, and a reused text gives an identical negative row (6,436
    duplicates in test). Why the avoided words (team decision, option (a) after P7 v0): TF-IDF
    learned the style of the benign texts; this step only changes which unused, length-matched
    text is chosen, so it needs no API call and keeps length matching and uniqueness.
    `length_matched` = the chosen text is within the window."""

    def in_window(c: dict[str, Any]) -> bool:
        return abs(c["length"] - attack_len) <= window * attack_len

    chosen = min(
        candidates,
        key=lambda c: (
            uses[c["benign_id"]],
            not in_window(c),
            avoid_score(c.get("text", ""), avoid),
            abs(c["length"] - attack_len),
            c["benign_id"],
        ),
    )
    return chosen, in_window(chosen)


def build_negatives(
    positives: list[dict[str, Any]],
    pool: dict[tuple[str, str], list[dict[str, Any]]],
    docs: dict[str, dict[str, Any]],
    cfg: Config,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Returns (negatives, pair records, failures)."""
    uses: Counter = Counter()
    avoid = frozenset(w.lower() for w in cfg.p4.avoid_words)
    negatives, pairs, failures = [], [], []
    for pos in positives:
        key = (pos["doc_id"], pos["position"])
        cands = pool.get(key)
        if not cands:
            failures.append(f"no pool texts for {key[0]}/{key[1]}")
            continue
        window = cfg.p4.length_window[group_of(pos["scenario"])]
        attack_len = len(pos["injection_span"])
        chosen, matched = pick(attack_len, cands, uses, window, avoid)
        uses[chosen["benign_id"]] += 1
        doc = docs[pos["doc_id"]]
        text, idx = insert(doc["document"], chosen["text"], pos["position"], doc["middle_index"])
        if idx != pos["insert_index"]:
            failures.append(f"{pos['sample_id']}: insert_index {idx} != {pos['insert_index']}")
        h = pos["sample_id"].removeprefix("bipia-pos-")
        neg = Sample(
            sample_id=f"bipia-neg-{h}",
            parent_id=pos["parent_id"],
            source="bipia",
            split=pos["split"],
            source_split=pos["source_split"],
            scenario=pos["scenario"],
            instruct=pos["instruct"],
            query=pos["query"],
            document=text,
            label=0,
            answer="no",
            label_provenance="construction",
            generation_method="llm_benign_insertion",
            position=pos["position"],
            insert_index=idx,
            benign_id=chosen["benign_id"],
            benign_span=[idx, idx + len(chosen["text"])],
            label_checked=bool(chosen.get("label_checked", False)),
        ).model_dump()
        negatives.append(neg | {"doc_id": pos["doc_id"], "benign_text": chosen["text"]})
        pairs.append(
            {
                "parent_id": pos["parent_id"],
                "split": pos["split"],
                "scenario": pos["scenario"],
                "position": pos["position"],
                "doc_id": pos["doc_id"],
                "attack_id": pos["attack_id"],
                "benign_id": chosen["benign_id"],
                "attack_length": attack_len,
                "benign_length": chosen["length"],
                "length_diff": chosen["length"] - attack_len,
                "length_matched": matched,
                "use_number": uses[chosen["benign_id"]],
                "over_size_cap": bool(chosen.get("over_size_cap", False)),
            }
        )
    return negatives, pairs, failures


# --------------------------------------------------------------------------- checks


def check_pairs(
    positives: list[dict[str, Any]],
    negatives: list[dict[str, Any]],
    docs: dict[str, dict[str, Any]],
    attacks: list[dict[str, Any]],
    attack_index: AttackIndex,
) -> list[str]:
    failures: list[str] = []
    neg_by_parent: dict[str, list[dict]] = defaultdict(list)
    for n in negatives:
        neg_by_parent[n["parent_id"]].append(n)

    not_one, differ, not_clean, no_insertion = [], [], [], []
    for p in positives:
        ns = neg_by_parent.get(p["parent_id"], [])
        if len(ns) != 1:
            not_one.append(p["parent_id"])
            continue
        n = ns[0]
        same = (
            "query",
            "instruct",
            "position",
            "insert_index",
            "split",
            "source_split",
            "scenario",
        )
        if any(p[k] != n[k] for k in same):
            differ.append(p["parent_id"])
        clean = docs[p["doc_id"]]["document"]
        try:
            ok = (
                remove(p["document"], p["injection_span"], p["position"], p["insert_index"])
                == clean
                == remove(n["document"], n["benign_text"], n["position"], n["insert_index"])
            )
        except InsertionError:
            ok = False
        if not ok:
            not_clean.append(p["parent_id"])
        if not n["benign_text"].strip() or n["document"] == clean:
            no_insertion.append(p["parent_id"])
    if not_one:
        failures.append(f"{len(not_one)} positives without exactly one negative")
    if differ:
        failures.append(f"{len(differ)} pairs differ in query/instruct/position/insert_index/split")
    if not_clean:
        failures.append(
            f"{len(not_clean)} pairs where removing the insertion does not give back "
            "the same clean document"
        )
    if no_insertion:
        failures.append(f"{len(no_insertion)} negatives without an insertion")

    # No negative contains any BIPIA attack text. The clean documents are checked once; in a
    # negative, an attack string could then only appear across the insertion, so the benign
    # text and the region around it (one maximum attack length on each side) are checked.
    attack_texts = [t for t in (normalise(a["text"]) for a in attacks) if t]
    span = max((len(a["text"]) for a in attacks), default=0)
    dirty_docs = {
        d
        for d in {n["doc_id"] for n in negatives}
        if any(t in normalise(docs[d]["document"]) for t in attack_texts)
    }
    if dirty_docs:
        failures.append(f"{len(dirty_docs)} clean documents contain BIPIA attack text")
    bad = []
    for n in negatives:
        i, j = n["insert_index"], n["insert_index"] + len(n["benign_text"])
        region = normalise(n["document"][max(0, i - span) : j + span])
        if attack_index.overlap(n["benign_text"]) is not None or any(
            t in region for t in attack_texts
        ):
            bad.append(n["sample_id"])
    if bad:
        failures.append(f"{len(bad)} negatives contain BIPIA attack text: {bad[:5]}")

    # 1:1 and balanced positions per split and scenario.
    for split in SPLITS:
        for s in SCENARIOS:
            pos_n = sum(1 for p in positives if p["split"] == split and p["scenario"] == s)
            neg_n = sum(1 for n in negatives if n["split"] == split and n["scenario"] == s)
            if pos_n != neg_n:
                failures.append(f"{split}/{s}: {pos_n} positives vs {neg_n} negatives")
            pc = Counter(
                n["position"] for n in negatives if n["split"] == split and n["scenario"] == s
            )
            if pc and max(pc.values()) - min(pc.values()) > 1:
                failures.append(f"{split}/{s}: positions not balanced {dict(pc)}")

    # No two rows identical (same document and query) within a split, for each class.
    for split in SPLITS:
        for rows, name in ((positives, "positive"), (negatives, "negative")):
            seen = Counter((r["document"], r["query"]) for r in rows if r["split"] == split)
            if dup := sum(n - 1 for n in seen.values() if n > 1):
                failures.append(f"{split}: {dup} duplicate {name} rows (same document and query)")

    # The same document never in two splits.
    splits_of: dict[str, set[str]] = defaultdict(set)
    for r in positives + negatives:
        splits_of[r["doc_id"]].add(r["split"])
    if multi := [d for d, s in splits_of.items() if len(s) > 1]:
        failures.append(f"{len(multi)} documents in more than one split")
    return failures


def counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"total": len(rows), "by_split": {}}
    for split in SPLITS:
        sub = [r for r in rows if r["split"] == split]
        out["by_split"][split] = {
            "total": len(sub),
            "scenario": dict(Counter(r["scenario"] for r in sub)),
            "label": {str(k): v for k, v in Counter(r["label"] for r in sub).items()},
        }
    return out


# --------------------------------------------------------------------------- report


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def _rate(a: float, b: float) -> str:
    return f"{a / b:.1%}" if b else "-"


def _esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "⏎")


def _sections(
    cfg: Config,
    rows: list[dict[str, Any]],
    positives: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    pool: dict[tuple[str, str], list[dict[str, Any]]],
    docs: dict[str, dict[str, Any]],
    status: list[str],
) -> list[tuple[str, str]]:
    # Composition.
    comp = []
    for split in SPLITS:
        for s in SCENARIOS:
            sub = [r for r in rows if r["split"] == split and r["scenario"] == s]
            if not sub:
                continue
            lab = Counter(r["label"] for r in sub)
            pos = Counter(r["position"] for r in sub)
            comp.append(
                [split, s, len(sub), lab[1], lab[0], pos["start"], pos["middle"], pos["end"]]
            )
    ag = Counter((p["split"], p["attack_group"]) for p in positives)
    ag_rows = []
    for split in SPLITS:
        groups = sorted({g for (sp, g) in ag if sp == split})
        vals = [ag[(split, g)] for g in groups]
        if vals:
            ag_rows.append(
                [
                    split,
                    len(groups),
                    min(vals),
                    max(vals),
                    ", ".join(f"{g}: {ag[(split, g)]}" for g in groups),
                ]
            )

    # Lengths.
    len_rows = []
    for split in SPLITS:
        for s in SCENARIOS:
            sub = [p for p in pairs if p["split"] == split and p["scenario"] == s]
            if not sub:
                continue
            fb = [p for p in sub if not p["length_matched"]]
            n_matched = sum(p["length_matched"] for p in sub)
            len_rows.append(
                [
                    split,
                    s,
                    len(sub),
                    f"{median(abs(p['length_diff']) for p in sub):.0f}",
                    f"{median(p['length_diff'] for p in sub):+.0f}",
                    f"{n_matched} ({_rate(n_matched, len(sub))})",
                    len(fb),
                    f"{median(abs(p['length_diff']) for p in fb):.0f}" if fb else "-",
                    max((abs(p["length_diff"]) for p in fb), default="-"),
                ]
            )
    all_abs = median(abs(p["length_diff"]) for p in pairs) if pairs else 0

    # Reuse.
    uses = Counter(p["benign_id"] for p in pairs)
    reuse_rows = []
    for split in SPLITS:
        for s in SCENARIOS:
            keys = {
                (p["doc_id"], p["position"])
                for p in pairs
                if p["split"] == split and p["scenario"] == s
            }
            if not keys:
                continue
            texts = [t for k in keys for t in pool[k]]
            used = [uses[t["benign_id"]] for t in texts if uses[t["benign_id"]]]
            reuse_rows.append(
                [
                    split,
                    s,
                    len(texts),
                    len(used),
                    len(texts) - len(used),
                    f"{sum(used) / len(used):.2f}" if used else "-",
                    max(used, default=0),
                ]
            )
    short = [(k, len(v)) for k, v in sorted(pool.items()) if len(v) < cfg.p3.n]
    negs_per_pool = Counter((p["doc_id"], p["position"]) for p in pairs)
    short_lines = (
        "\n".join(
            f"- `{k[0]}` / {k[1]}: {n} pool texts, {negs_per_pool[k]} negatives, uses per "
            f"text: {sorted((uses[t['benign_id']] for t in pool[k]), reverse=True)}"
            for k, n in short
        )
        or "- None."
    )

    # P3 rejection statistics.
    p3 = {}
    p3_path = report_path(cfg, "p3_rejections.json")
    if p3_path.is_file():
        p3 = json.loads(p3_path.read_text(encoding="utf-8"))
    p3_rows = []
    for f, name in ((1, "shape"), (2, "forbidden content"), (3, "duplicate"), (4, "judge")):
        reach = sum(v for k, v in p3.get("reach", {}).items() if k.endswith(f"|{f}"))
        drop = sum(v for k, v in p3.get("rejected", {}).items() if k.endswith(f"|{f}"))
        p3_rows.append([f, name, reach, drop, _rate(drop, reach)])

    # Examples.
    ex = []
    for s in SCENARIOS:
        p = next((p for p in positives if p["scenario"] == s and p["split"] == "test"), None)
        if p is None:
            continue
        pr = next(x for x in pairs if x["parent_id"] == p["parent_id"])
        btext = next(
            t["text"]
            for t in pool[(p["doc_id"], p["position"])]
            if t["benign_id"] == pr["benign_id"]
        )
        ex.append(
            f"**{s}** (`{p['parent_id']}`, position={p['position']}, insert_index="
            f"{p['insert_index']})\n\n"
            f"- label 1, attack ({len(p['injection_span'])} chars): "
            f"{_esc(p['injection_span'][:160])}\n"
            f"- label 0, benign ({len(btext)} chars): {_esc(btext[:160])}"
        )

    passed = [
        "- PASSED: classes 1:1 within each split and scenario; every positive has exactly one "
        "negative with the same parent_id",
        "- PASSED: positions balanced within 1 per split and scenario",
        "- PASSED: within each pair only the inserted text differs (same query, instruct, "
        "position, insert_index; removing the insertion gives the same clean document)",
        "- PASSED: no negative contains any BIPIA attack text",
        "- PASSED: every negative has an insertion (tables included)",
        "- PASSED: no duplicate rows (same document and query) within a split, for either class",
        "- PASSED: no document in two splits; counts match `expected_counts.p4_bipia`",
    ]
    n_fb = sum(not p["length_matched"] for p in pairs)
    return [
        (
            "Composition (split x scenario x class x position)",
            _table(
                ["split", "scenario", "rows", "label 1", "label 0", "start", "middle", "end"], comp
            ),
        ),
        (
            "Attack groups (positives; each negative mirrors its positive)",
            _table(["split", "attack groups", "min", "max", "rows per group"], ag_rows),
        ),
        (
            "Length: positive vs negative",
            f"Within a pair the document and the position are identical, so the length "
            f"difference is len(benign text) - len(attack). Median absolute difference over all "
            f"pairs: **{all_abs:.0f} characters** (brief's pilot expectation: about 6).\n\n"
            + _table(
                [
                    "split",
                    "scenario",
                    "pairs",
                    "median |diff|",
                    "median diff",
                    "length-matched",
                    "fallback pairs",
                    "fallback median |diff|",
                    "fallback max |diff|",
                ],
                len_rows,
            )
            + f"\n\n*Length-matched* = the pool text is within {cfg.p4.length_window['text']:.0%} "
            f"of the attack's length ({cfg.p4.length_window['code']:.0%} for code). "
            f"**{n_fb} pairs ({_rate(n_fb, len(pairs))})** are not length-matched: they are "
            "flagged `length_matched: false` in `data/interim/p4_pairs.jsonl`.\n\n"
            "**Picking order (team decision, option B):** least used first, then a text within "
            "the window, then the closest length. The brief puts the window first. Why: with the "
            "window first, the few texts close to a long attack were picked again and again (one "
            "test text 13 times in its pool), and a reused text gives an identical negative row: "
            "6,436 duplicate negatives in test (30%), 346 in train, 70 in validation. With least "
            "used first, and the test email/table pools extended to 25 texts in P3, no text is "
            "reused while an unused one remains, so there are no duplicate rows.",
        ),
        (
            "Pool text reuse",
            "Least used first (see *Length*). Test email/table pools have 25 texts for 25 "
            "positives (P3 extension), the other pools 20 texts for at most 17 positives, so a "
            "text is only reused when its pool has none left unused.\n\n"
            + _table(
                [
                    "split",
                    "scenario",
                    "pool texts",
                    "used",
                    "never used",
                    "mean uses (used)",
                    "max uses",
                ],
                reuse_rows,
            )
            + "\n\nApproved short pools (from P3):\n\n"
            + short_lines,
        ),
        (
            "P3 rejection statistics (pool texts)",
            _table(["filter", "name", "texts checked", "rejected", "rate"], p3_rows)
            + f"\n\nP3 top-up: {len(p3.get('topped_up', {}))} pools took set-aside texts over the "
            f"size cap; approved short pools: {len(p3.get('accepted_short_pools', []))}. Details "
            "in `reports/p3/p3_rejections.md`.",
        ),
        (
            "Notes from P3, and what P4 did",
            "1. **Small pools** (`bipia-code-7f1bb6f33328`: start 18 texts for 16 samples, end 17 "
            "for 17). P4's least-used picking spreads them; the uses per text are listed under "
            "*Pool text reuse*.\n"
            "2. **Length gap** (P3 email/table texts median 48 characters vs 63 for the attacks). "
            "The length window pairs each attack with a pool text of similar length; the "
            "resulting gap is in *Length: positive vs negative*.",
        ),
        ("Example pairs (test)", "\n\n".join(ex)),
        ("Checks", "\n".join([f"- FAILED: {f}" for f in status] or passed)),
        (
            "Deviations from the brief",
            "\n".join(
                [
                    "- Picking order: least used first, then the length window (the brief puts "
                    "the window first). Why: the window-first order produced 6,436 duplicate "
                    "negative rows in test. Team decision (option B), together with the P3 "
                    "extension of the test email/table pools to 25 texts.",
                    f"- Length window not met for {n_fb} pairs ({_rate(n_fb, len(pairs))}), "
                    "flagged `length_matched: false` in `data/interim/p4_pairs.jsonl`.",
                    "- Negatives have `attack_group`, `attack_name`, `injection_span` and "
                    "`protected_tokens` empty: they contain no attack. `parent_id` links each "
                    "negative to its positive and its attack.",
                    "- `length_matched`, the length difference and the use count are not contract "
                    "columns; they are kept in `data/interim/p4_pairs.jsonl`, not in data/out.",
                    "- Two P3 pools of `bipia-code-7f1bb6f33328` have 18 and 17 texts (approved in "
                    "P3); they cover their positives.",
                ]
            ),
        ),
    ]


def _style_section(
    pairs: list[dict[str, Any]], pool: dict[tuple[str, str], list[dict[str, Any]]]
) -> list[tuple[str, str]]:
    """Share of negatives whose benign text is a hard negative (P3 option B, if present)."""
    style = {t["benign_id"]: t.get("style", "standard") for ts in pool.values() for t in ts}
    if "hard_negative" not in style.values():
        return []
    c: Counter = Counter()
    n: Counter = Counter()
    for pr in pairs:
        key = (pr["split"], pr["scenario"])
        n[key] += 1
        c[key] += style.get(pr["benign_id"]) == "hard_negative"
    rows = [
        [sp, s, n[(sp, s)], c[(sp, s)], _rate(c[(sp, s)], n[(sp, s)])]
        for sp in SPLITS
        for s in SCENARIOS
        if n[(sp, s)]
    ]
    return [
        (
            "Hard negatives among the chosen benign texts (P3 option B)",
            _table(["split", "scenario", "negatives", "hard negatives", "share"], rows)
            + "\n\nAbout 40% of every pool are hard negatives (harmless texts that sound like "
            "instructions to the human reader); P4's picking order is unchanged.",
        )
    ]


# --------------------------------------------------------------------------- run


def run(cfg: Config | None = None, force: bool = False) -> dict[str, Any]:
    cfg = cfg or load_config()
    positives = load_positives(cfg)
    docs = {d["doc_id"]: d for d in read_jsonl(cfg.paths.interim / DOCS_FILE)}
    attacks = list(read_jsonl(cfg.paths.interim / ATTACKS_FILE))
    pool: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in read_jsonl(cfg.paths.interim / POOL_FILE):
        pool[(t["doc_id"], t["position"])].append(t)
    for k in pool:
        pool[k].sort(key=lambda t: t["benign_id"])

    negatives, pairs, failures = build_negatives(positives, pool, docs, cfg)
    failures += check_pairs(
        positives, negatives, docs, attacks, AttackIndex.build(attacks, cfg.p3.attack_overlap_words)
    )
    rows = [{c: p.get(c) for c in COLUMNS} for p in positives] + [
        {c: n[c] for c in COLUMNS} for n in negatives
    ]
    try:
        check_expected("p4_bipia", counts(rows), cfg=cfg)
    except CountMismatchError as e:
        failures.append(str(e))

    write_counts_report(
        STEP,
        rows,
        keys=("split", "scenario", "label", "position"),
        cfg=cfg,
        sections=_sections(cfg, rows, positives, pairs, pool, docs, failures)
        + _style_section(pairs, pool),
        name=REPORT_NAME,
        extra={
            "p4_bipia": counts(rows),
            "failures": failures,
            "length_matched": sum(p["length_matched"] for p in pairs),
            "pairs": len(pairs),
        },
    )
    report = report_path(cfg, f"{REPORT_NAME}.md")
    if failures:
        raise P4CheckError(
            f"P4 stopped, data/out not written. See {report}:\n  " + "\n  ".join(failures)
        )

    # Refuse to overwrite data/out without --force, for all splits, before writing any.
    paths = {split: prepare_out(out_path(cfg, split), force=force, cfg=cfg) for split in SPLITS}
    for split, path in paths.items():
        sub = sorted(
            (r for r in rows if r["split"] == split), key=lambda r: (r["parent_id"], -r["label"])
        )
        n = write_parquet(path, [Sample(**r) for r in sub])
        logger.info("Wrote %d %s rows -> %s", n, split, path)
    pairs_path = prepare_out(cfg.paths.interim / PAIRS_FILE, force=force, cfg=cfg)
    write_jsonl(pairs_path, pairs)
    return {
        "paths": paths,
        "report": report,
        "rows": len(rows),
        "pairs": len(pairs),
        "fallback": sum(not p["length_matched"] for p in pairs),
    }
