"""P7: data checks, length gap, shortcut baselines and diagnostics, once per version.

P7 only reads data/out (plus the interim and raw files it compares against) and writes
reports/p7/p7_checks_<version>.{md,json}. It never changes the data, never loads a model and
never changes a threshold. The push (P10) for a version needs the report to say PUSH ALLOWED.

v0 = `bipia` (train, validation, test) + `nemotron` (test). v1 = the same data with `query` = the
user request only and no `instruct` column. v2 and v3 add the TextFooler configs.
"""

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from typing import Any

import pyarrow.parquet as pq

from dataset_builder.config import Config, load_config
from dataset_builder.insert import SEP, InsertionError, remove
from dataset_builder.io import arrow_schema, read_jsonl
from dataset_builder.query import query as build_doc_query
from dataset_builder.reports import report_path
from dataset_builder.schema import Sample, TextFoolerSample, columns_for

SCOPE: dict[str, dict[str, tuple[str, ...]]] = {
    "v0": {"bipia": ("train", "validation", "test"), "nemotron": ("test",)},
    # v1: the v0 data with `query` = the user request only and no `instruct` column
    "v1": {"bipia": ("train", "validation", "test"), "nemotron": ("test",)},
    "v2": {
        "bipia": ("train", "validation", "test"),
        "nemotron": ("test",),
        "bipia_textfooler": ("test",),
    },
}
LATER = {"v3": "needs P8 (bipia_textfooler train/val)"}
POSITIONS = ("start", "middle", "end")
SCEN_ORDER = ("email", "table", "code")
PRIORITY_MARKERS = ("[PRIORITY MESSAGE]", "[/PRIORITY MESSAGE]")
URGENT_FRAME = re.compile(r"URGENT:.*?This is a critical priority\.", re.DOTALL)

Rows = list[dict[str, Any]]
EXIT_OK, EXIT_DATA, EXIT_LIMIT = 0, 1, 2


class P7NotReadyError(RuntimeError):
    """The requested version's configs are not built yet."""


def result(
    part: str, name: str, config: str, bad: Iterable[str] | None, detail: str = ""
) -> dict[str, Any]:
    """bad = failing items (empty = PASS); None = n/a."""
    status = "n/a" if bad is None else ("FAIL" if list(bad) else "PASS")
    bad = list(bad or [])
    return {
        "part": part,
        "check": name,
        "config": config,
        "status": status,
        "detail": detail + (f" ({len(bad)} failing)" if bad else ""),
        "examples": bad[:5],
    }


# ================================================================ helpers


def doc_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inserted_text(r: dict[str, Any]) -> str:
    """BIPIA: the attack (positive) or the benign text at benign_span (negative), by offsets."""
    if r["label"] == 1:
        return r["injection_span"]
    s, e = r["benign_span"]
    return r["document"][s:e]


def clean_document(r: dict[str, Any]) -> str:
    """Remove the inserted text by offsets, with insert()'s positions and newline join."""
    return remove(r["document"], inserted_text(r), r["position"], r["insert_index"])


def bipia_pairs(rows: Rows) -> dict[str, dict[int, list[dict]]]:
    out: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        out[r["parent_id"]][r["label"]].append(r)
    return out


def nemotron_pairs(rows: Rows) -> dict[str, dict[int, list[dict]]]:
    """The negative's parent_id is the positive's sample_id."""
    out: dict[str, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        out[r["sample_id"] if r["label"] == 1 else r["parent_id"]][r["label"]].append(r)
    return out


def one_each(p: dict[int, list[dict]]) -> bool:
    return len(p[0]) == 1 and len(p[1]) == 1


def json_strings(document: str) -> list[str]:
    """Nemotron documents are JSON: their decoded string values (newlines are real again)."""
    from dataset_builder.steps.p5_nemotron import string_leaves

    try:
        return [v for _, v in string_leaves(json.loads(document))]
    except json.JSONDecodeError:
        return []


def contains_decoded(document: str, needle: str) -> bool:
    """Exact match in the raw document or in one of its decoded JSON strings."""
    return needle in document or any(needle in s for s in json_strings(document))


def percentile(xs: list[float], q: float) -> float:
    import numpy as np

    return float(np.percentile(xs, q)) if len(xs) else float("nan")


# ================================================================ part 1: data checks


def same_type(a: Any, b: Any) -> bool:
    import pyarrow as pa

    if pa.types.is_list(a) or pa.types.is_large_list(a):
        return (pa.types.is_list(b) or pa.types.is_large_list(b)) and same_type(
            a.value_type, b.value_type
        )
    return a == b


def check_load(
    cfg: Config, scope: dict[str, tuple[str, ...]], reports: dict[str, dict[str, int]]
) -> tuple[list[dict], dict[str, int]]:
    """1. Load with datasets; columns and types vs the contract; row counts vs P4/P5 reports."""
    os.environ["HF_DATASETS_OFFLINE"] = "1"  # local files only: no network call
    from datasets import load_dataset

    out, counts = [], {}
    with tempfile.TemporaryDirectory(prefix="p7") as cache:  # short path (Windows limit)
        for config, splits in scope.items():
            files = {s: str(cfg.paths.out / config / f"{s}.parquet") for s in splits}
            ds = load_dataset("parquet", data_files=files, cache_dir=cache)
            types_bad, count_bad = [], []
            names = columns_for(config)
            expected = arrow_schema(TextFoolerSample if config == "bipia_textfooler" else Sample)
            for s in splits:
                schema = ds[s].features.arrow_schema
                counts[f"{config}/{s}"] = ds[s].num_rows
                if tuple(schema.names) != names:
                    types_bad.append(f"{s}: column names or order differ from the contract")
                else:
                    types_bad += [
                        f"{s}.{f.name}: {schema.field(f.name).type} (contract {f.type})"
                        for f in expected
                        if not same_type(schema.field(f.name).type, f.type)
                    ]
                want = reports.get(config, {}).get(s)
                if want != ds[s].num_rows:
                    count_bad.append(f"{s}: {ds[s].num_rows} rows, report says {want}")
            out.append(
                result(
                    "1",
                    "load: columns and types match the contract",
                    config,
                    types_bad,
                    f"{len(names)} columns",
                )
            )
            out.append(
                result(
                    "1",
                    "load: row counts match the P4/P5 reports",
                    config,
                    count_bad,
                    ", ".join(f"{s} {counts[f'{config}/{s}']:,}" for s in splits),
                )
            )
    return out, counts


def check_splits(data: dict[str, Rows]) -> list[dict]:
    """2. No clean document (sha256 of the clean text) in two splits."""
    seen: dict[str, set[str]] = defaultdict(set)
    broken = []
    for split, rows in data.items():
        for r in rows:
            try:
                seen[doc_hash(clean_document(r))].add(split)
            except (InsertionError, TypeError, KeyError) as e:
                broken.append(f"{r['sample_id']}: {e}")
    multi = [f"{h[:12]}: {sorted(s)}" for h, s in seen.items() if len(s) > 1]
    return [
        result(
            "1",
            "splits: no clean document in two splits",
            "bipia",
            broken + multi,
            f"{len(seen):,} clean documents (sha256 of the clean text)",
        )
    ]


def check_attacks_in_test(
    data: dict[str, Rows], attacks: Rows, allowed_groups: Iterable[str] = ()
) -> list[dict]:
    """2. No attack_name or attack text used in train/validation appears in test. Names of an
    attack group on the team's allow-list (bipia.allowed_shared_attack_groups) are reported but
    allowed: same names, different strings (the attack text check still applies to them)."""
    used = [r for s in ("train", "validation") for r in data.get(s, []) if r["label"] == 1]
    names = {r["attack_name"] for r in used}
    texts = {r["injection_span"] for r in used} | {
        a["text"] for a in attacks if a["source_split"] == "train"
    }
    test = [r for r in data.get("test", []) if r["label"] == 1]
    allowed = set(allowed_groups)
    shared = Counter(
        (r["attack_name"], r["attack_group"]) for r in test if r["attack_name"] in names
    )
    by_name = {n: c for (n, g), c in shared.items() if g not in allowed}
    ok_names = sorted(n for (n, g) in shared if g in allowed)
    by_text = [r["sample_id"] for r in test if r["injection_span"] in texts]
    return [
        result(
            "1",
            "splits: no train/validation attack text in test",
            "bipia",
            by_text,
            f"{len(test):,} test positives vs {len(texts)} train/validation attack texts",
        ),
        result(
            "1",
            "splits: no train/validation attack_name in test",
            "bipia",
            [f"{n} ({c} test rows)" for n, c in sorted(by_name.items())],
            f"{len({r['attack_name'] for r in test})} test attack names"
            + (
                f"; allowed by team decision (same names, different strings): {', '.join(ok_names)}"
                if ok_names
                else ""
            ),
        ),
    ]


def check_positives(data: dict[str, Rows], nemotron: Rows) -> tuple[list[dict], list[dict]]:
    """3. BIPIA: injection_span exactly at insert_index. Nemotron: the injection text in decoded
    form. Also returns three examples of the JSON-newline case (evidence for the check fix)."""
    bad = [
        r["sample_id"]
        for rows in data.values()
        for r in rows
        if r["label"] == 1
        and r["document"][r["insert_index"] : r["insert_index"] + len(r["injection_span"])]
        != r["injection_span"]
    ]
    n = sum(r["label"] == 1 for rows in data.values() for r in rows)
    pos = [r for r in nemotron if r["label"] == 1]
    nbad = [
        r["sample_id"]
        for r in pos
        if not r["injection_span"] or not contains_decoded(r["document"], r["injection_span"])
    ]
    evidence = [
        {
            "sample_id": r["sample_id"],
            "raw_match": False,
            "decoded_match": True,
            "injection_start": r["injection_span"][:60],
        }
        for r in pos
        if r["injection_span"]
        and r["injection_span"] not in r["document"]
        and contains_decoded(r["document"], r["injection_span"])
    ][:3]
    return [
        result(
            "1",
            "positives: contain their injection_span exactly",
            "bipia",
            bad,
            f"{n:,} positives, at insert_index",
        ),
        result(
            "1",
            "positives: contain their injection text",
            "nemotron",
            nbad,
            f"{len(pos):,} positives; compared in decoded form (JSON strings)",
        ),
    ], evidence


def aho_corasick(patterns: Iterable[str]) -> Any:
    import ahocorasick

    auto = ahocorasick.Automaton()
    for i, p in enumerate(dict.fromkeys(patterns)):
        auto.add_word(p, (i, p))
    auto.make_automaton()
    return auto


def check_negatives(
    data: dict[str, Rows], attacks: Rows, nemotron: Rows, goals: dict[int, str]
) -> list[dict]:
    """4. BIPIA negatives contain no attack text (Aho-Corasick over all attacks). Nemotron
    negatives contain no injection text, goal sentence or wrapper marker."""
    from dataset_builder.steps.p5_nemotron import _contains

    auto = aho_corasick(a["text"] for a in attacks)
    negs = [r for rows in data.values() for r in rows if r["label"] == 0]
    dirty = [r["sample_id"] for r in negs if next(auto.iter(r["document"]), None) is not None]
    out = [
        result(
            "1",
            "negatives: no attack text",
            "bipia",
            dirty,
            f"{len(negs):,} negatives vs {len(attacks)} attacks (Aho-Corasick)",
        )
    ]
    bad = []
    for p in nemotron_pairs(nemotron).values():
        if not one_each(p):
            continue  # reported by the pair check
        neg, pos = p[0][0], p[1][0]
        texts = [neg["document"], *json_strings(neg["document"])]
        rid = int(pos["sample_id"].split("-")[1])
        why = []
        if contains_decoded(neg["document"], pos["injection_span"]):
            why.append("injection text")
        if rid in goals and _contains(neg["document"], goals[rid]):
            why.append("goal sentence")
        if any(m in t for t in texts for m in PRIORITY_MARKERS) or any(
            URGENT_FRAME.search(t) for t in texts
        ):
            why.append("wrapper marker")
        if why:
            bad.append(f"{neg['sample_id']}: {', '.join(why)}")
    n = sum(r["label"] == 0 for r in nemotron)
    out.append(
        result(
            "1",
            "negatives: no injection text, goal sentence or wrapper marker",
            "nemotron",
            bad,
            f"{n:,} negatives; goals from the raw records ({len(goals):,})",
        )
    )
    return out


def check_balance(cfg: Config, config: str, data: dict[str, Rows]) -> tuple[list[dict], list]:
    """5. Classes 1:1 per scenario; position shares within tolerance of one third."""
    cls_bad, pos_bad, shares = [], [], []
    for split, rows in data.items():
        by: dict[str, Counter] = defaultdict(Counter)
        pos: dict[tuple[str, int], Counter] = defaultdict(Counter)
        for r in rows:
            by[r["scenario"]][r["label"]] += 1
            if r.get("position"):
                pos[(r["scenario"], r["label"])][r["position"]] += 1
        cls_bad += [f"{split}/{s}: {c[1]} vs {c[0]}" for s, c in sorted(by.items()) if c[1] != c[0]]
        tol = cfg.p7.position_tolerance.get(split, 0.05)
        for (s, lab), c in sorted(pos.items()):
            n = sum(c.values())
            row = {"split": split, "scenario": s, "label": lab, "rows": n}
            row |= {p: c.get(p, 0) / n for p in POSITIONS}
            shares.append(row)
            off = [p for p in POSITIONS if abs(row[p] - 1 / 3) > tol + 1e-12]
            if off:
                pos_bad.append(f"{split}/{s}/{lab}: " + ", ".join(f"{p} {row[p]:.1%}" for p in off))
    out = [
        result(
            "1",
            "balance: classes 1:1 within each scenario",
            config,
            cls_bad,
            "per split and scenario",
        )
    ]
    tol = cfg.p7.position_tolerance
    out.append(
        result(
            "1",
            "balance: positions within tolerance of one third",
            config,
            pos_bad if config == "bipia" else None,
            f"tolerance {tol.get('test', 0):.0%} on test, {tol.get('train', 0):.0%} on "
            "train/validation"
            if config == "bipia"
            else "no insertion positions",
        )
    )
    return out, shares


def check_pairs(data: dict[str, Rows], docs: Rows) -> list[dict]:
    """6. Removing the inserted text by offsets from both members gives the same clean document,
    equal to the one stored in data/interim/bipia_docs.jsonl."""
    stored = {(d["split"], doc_hash(d["document"])) for d in docs}
    bad, n = [], 0
    for split, rows in data.items():
        for key, p in bipia_pairs(rows).items():
            n += 1
            if not one_each(p):
                bad.append(f"{split}/{key}: {len(p[1])} positive(s), {len(p[0])} negative(s)")
                continue
            try:
                a, b = clean_document(p[1][0]), clean_document(p[0][0])
            except (InsertionError, TypeError, KeyError) as e:
                bad.append(f"{split}/{key}: {e}")
                continue
            if a != b:
                bad.append(f"{split}/{key}: clean documents differ")
            elif (split, doc_hash(a)) not in stored:
                bad.append(f"{split}/{key}: clean document not in bipia_docs.jsonl")
    return [
        result(
            "1",
            "pairs: same clean document, equal to bipia_docs.jsonl",
            "bipia",
            bad,
            f"{n:,} pairs; offsets (injection_span / benign_span), newline join {SEP!r}",
        )
    ]


def check_queries(cfg: Config, data: dict[str, Rows], docs: Rows) -> list[dict]:
    """7. Same query in every pair; test query = the document's first question."""
    by_hash = {(d["split"], doc_hash(d["document"])): d for d in docs}
    differ, not_first = [], []
    for split, rows in data.items():
        for key, p in bipia_pairs(rows).items():
            if not one_each(p):
                continue  # reported by the pair check
            pos, neg = p[1][0], p[0][0]
            if pos["query"] != neg["query"]:
                differ.append(f"{split}/{key}")
            if split == "test":
                try:
                    d = by_hash.get((split, doc_hash(clean_document(pos))))
                except (InsertionError, TypeError, KeyError):
                    d = None
                if d is None or pos["query"] != build_doc_query(d, cfg):
                    not_first.append(f"{split}/{key}")
    return [
        result(
            "1",
            "query: identical query in every pair",
            "bipia",
            differ,
            "positive vs negative",
        ),
        result(
            "1",
            "query: test uses the document's first question",
            "bipia",
            not_first,
            "rebuilt from bipia_docs.jsonl with the same template",
        ),
    ]


# ================================================================ part 2: length gap


def length_gaps(config: str, data: dict[str, Rows]) -> list[dict]:
    gaps: dict[tuple[str, str], list[int]] = defaultdict(list)
    pairs_of = bipia_pairs if config == "bipia" else nemotron_pairs
    for split, rows in data.items():
        for p in pairs_of(rows).values():
            if one_each(p):
                d = len(p[1][0]["document"]) - len(p[0][0]["document"])
                gaps[(split, p[1][0]["scenario"])].append(d)
    return [
        {
            "config": config,
            "split": sp,
            "scenario": s,
            "pairs": len(v),
            "median": percentile(v, 50),
            "p5": percentile(v, 5),
            "p95": percentile(v, 95),
        }
        for (sp, s), v in sorted(gaps.items())
    ]


# ================================================================ part 3: baselines


def evaluate(y: list[int], scores: list[float], fpr: float) -> dict[str, Any]:
    """AUC, and recall at `fpr`: threshold = the (1 - fpr) percentile of the negatives' scores,
    recall = share of positives scoring above it."""
    from sklearn.metrics import roc_auc_score

    neg = [s for s, t in zip(scores, y, strict=True) if t == 0]
    pos = [s for s, t in zip(scores, y, strict=True) if t == 1]
    if not neg or not pos:
        return {"auc": None, "recall": None, "n": len(y)}
    thr = percentile(neg, 100 * (1 - fpr))
    return {
        "auc": float(roc_auc_score(y, scores)),
        "recall": sum(s > thr for s in pos) / len(pos),
        "n": len(y),
    }


def by_group(rows: Rows, scores: Any, fpr: float, key: Callable[[dict], str | None]) -> dict:
    groups: dict[str, list[int]] = {"all": list(range(len(rows)))}
    for i, r in enumerate(rows):
        k = key(r)
        if k is not None:
            groups.setdefault(k, []).append(i)
    return {
        g: evaluate([rows[i]["label"] for i in idx], [float(scores[i]) for i in idx], fpr)
        for g, idx in groups.items()
    }


def _scenario(r: dict[str, Any]) -> str:
    return r["scenario"]


def length_features(rows: Rows) -> Any:
    import numpy as np

    return np.array(
        [[math.log1p(len(r["document"])), math.log1p(len(r["document"].split()))] for r in rows]
    )


def tfidf_vectorizer(cfg: Config) -> Any:
    from sklearn.feature_extraction.text import TfidfVectorizer

    t = cfg.p7.tfidf
    return TfidfVectorizer(
        ngram_range=tuple(t.ngram),
        min_df=t.min_df,
        max_features=t.max_features,
        sublinear_tf=True,
    )


def logreg(cfg: Config) -> Any:
    from sklearn.linear_model import LogisticRegression

    return LogisticRegression(C=cfg.p7.tfidf.C, max_iter=5000, random_state=cfg.seed)


def without_groups(rows: Rows, groups: Iterable[str]) -> Rows:
    """BIPIA rows without the pairs whose attack is in `groups` (both members of the pair)."""
    groups = set(groups)
    drop = {r["parent_id"] for r in rows if r["label"] == 1 and r["attack_group"] in groups}
    return [r for r in rows if r["parent_id"] not in drop]


def baselines(cfg: Config, train: Rows, test: Rows, nemotron: Rows) -> dict[str, Any]:
    fpr = cfg.p7.fpr
    no_lt = without_groups(test, cfg.bipia.allowed_shared_attack_groups)
    y = [r["label"] for r in train]
    length = logreg(cfg).fit(length_features(train), y)
    vec = tfidf_vectorizer(cfg)
    xtr = vec.fit_transform([r["document"] for r in train])
    tfidf = logreg(cfg).fit(xtr, y)
    xte = vec.transform([r["document"] for r in test])
    xnm = vec.transform([r["document"] for r in nemotron])
    other = "bipia test without " + " / ".join(cfg.bipia.allowed_shared_attack_groups)
    return {
        "length": {
            "bipia test": by_group(
                test, length.predict_proba(length_features(test))[:, 1], fpr, _scenario
            ),
            other: by_group(
                no_lt, length.predict_proba(length_features(no_lt))[:, 1], fpr, _scenario
            ),
        },
        "tfidf": {
            "bipia test": by_group(test, tfidf.predict_proba(xte)[:, 1], fpr, _scenario),
            other: by_group(
                no_lt,
                tfidf.predict_proba(vec.transform([r["document"] for r in no_lt]))[:, 1],
                fpr,
                _scenario,
            ),
            "nemotron test": by_group(nemotron, tfidf.predict_proba(xnm)[:, 1], fpr, _scenario),
        },
        "_models": {"vec": vec, "tfidf": tfidf, "xtr": xtr, "xte": xte},
    }


def nemotron_length(rows: Rows) -> dict[str, Any]:
    """Report only: AUC of the raw document length (longer = attack), pooled and per domain,
    and the share of pairs in which the positive is longer."""
    from sklearn.metrics import roc_auc_score

    out: dict[str, Any] = {}
    groups: dict[str, Rows] = {"all": rows}
    for r in rows:
        groups.setdefault(r["scenario"], []).append(r)
    for g, rs in groups.items():
        pairs = [p for p in nemotron_pairs(rs).values() if one_each(p)]
        longer = sum(len(p[1][0]["document"]) > len(p[0][0]["document"]) for p in pairs)
        labels = [r["label"] for r in rs]
        out[g] = {
            "auc": float(roc_auc_score(labels, [len(r["document"]) for r in rs]))
            if len(set(labels)) == 2
            else None,
            "positive_longer": longer / len(pairs) if pairs else None,
            "pairs": len(pairs),
        }
    return out


def stop_rule(cfg: Config, base: dict[str, Any]) -> list[str]:
    """Limits from the config, per scenario on BIPIA (both baselines) and on the pooled TF-IDF
    result on Nemotron. P7 never edits them."""
    limits = {"length": cfg.p7.length_auc_stop, "tfidf": cfg.p7.tfidf_auc_stop}
    hits = []
    for name in ("length", "tfidf"):
        for g, m in base[name]["bipia test"].items():
            if g != "all" and m["auc"] is not None and m["auc"] > limits[name]:
                hits.append(f"{name} on bipia test/{g}: AUC {m['auc']:.3f} > {limits[name]}")
    m = base["tfidf"]["nemotron test"]["all"]
    if m["auc"] is not None and m["auc"] > limits["tfidf"]:
        hits.append(f"tfidf on nemotron test: AUC {m['auc']:.3f} > {limits['tfidf']}")
    return hits


# ================================================================ part 4: diagnostics


def hard_negative_view(
    cfg: Config, test: Rows, scores: Any, all_rows: Rows, style: dict[str, str]
) -> dict[str, Any]:
    """Share of negatives whose benign text is a hard negative (P3 option B), and the TF-IDF AUC
    on BIPIA test with only hard-negative or only standard negatives (all positives)."""
    if not style:
        return {}
    share: dict[str, dict[str, float]] = defaultdict(dict)
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for r in all_rows:
        if r["label"] == 0:
            groups[(r["split"], r["scenario"])].append(style.get(r["benign_id"]) == "hard_negative")
    for (sp, s), v in sorted(groups.items()):
        share[sp][s] = sum(v) / len(v)
    auc: dict[str, dict[str, Any]] = {}
    for kind in ("hard_negative", "standard"):
        idx = [
            i
            for i, r in enumerate(test)
            if r["label"] == 1 or style.get(r["benign_id"], "standard") == kind
        ]
        rows = [test[i] for i in idx]
        auc[kind] = by_group(rows, [scores[i] for i in idx], cfg.p7.fpr, _scenario)
    return {"share": dict(share), "auc": auc}


def diagnostics(
    cfg: Config,
    train: Rows,
    test: Rows,
    base: dict[str, Any],
    all_rows: Rows,
    style: dict[str, str] | None = None,
) -> dict[str, Any]:
    import numpy as np
    from sklearn.feature_extraction.text import CountVectorizer

    m = base["_models"]
    vec, model, xtr, xte = m["vec"], m["tfidf"], m["xtr"], m["xte"]
    names = vec.get_feature_names_out()
    coef = model.coef_[0]
    fpr = cfg.p7.fpr

    # Origin of a feature: share of the train rows containing it (in their document) that also
    # contain it inside their inserted span (injection_span for label 1, benign_span for 0).
    binary = CountVectorizer(
        vocabulary=vec.vocabulary_, ngram_range=tuple(cfg.p7.tfidf.ngram), binary=True
    )
    share = {}
    for lab in (1, 0):
        rows = [r for r in train if r["label"] == lab]
        in_doc = np.asarray(binary.transform([r["document"] for r in rows]).sum(axis=0))[0]
        in_span = np.asarray(binary.transform([inserted_text(r) for r in rows]).sum(axis=0))[0]
        share[lab] = np.divide(
            in_span, in_doc, out=np.zeros(len(names), dtype=float), where=in_doc > 0
        )

    def origin(i: int, lab: int) -> str:
        if share[lab][i] >= 0.5:
            return "injection_span" if lab == 1 else "benign_span"
        return "document"

    # 1. Strongest features per scenario and class: weight x mean tf-idf in that group (test).
    top: dict[str, dict[str, list]] = {}
    for s in sorted({r["scenario"] for r in test}):
        top[s] = {}
        for lab in (1, 0):
            idx = [i for i, r in enumerate(test) if r["scenario"] == s and r["label"] == lab]
            mean = np.asarray(xte[idx].mean(axis=0))[0]
            contrib = coef * mean * (1 if lab == 1 else -1)
            top[s][str(lab)] = [
                {
                    "feature": str(names[i]),
                    "weight": float(coef[i]),
                    "contribution": float(contrib[i]),
                    "origin": origin(i, lab),
                    "span_share": float(share[lab][i]),
                }
                for i in np.argsort(-contrib, kind="stable")[:30]
                if contrib[i] > 0
            ]

    # 2. Ablation: retrain without the 50 strongest negative-class benign_span features.
    neg_order = [
        i for i in np.argsort(coef, kind="stable") if coef[i] < 0 and origin(i, 0) == "benign_span"
    ]
    drop = neg_order[:50]
    keep = np.ones(len(names), dtype=bool)
    keep[drop] = False
    ablated_model = logreg(cfg).fit(xtr[:, keep], [r["label"] for r in train])
    ablated = by_group(test, ablated_model.predict_proba(xte[:, keep])[:, 1], fpr, _scenario)
    ablation = {
        "dropped": [str(names[i]) for i in drop],
        "auc": {
            g: {"before": base["tfidf"]["bipia test"][g]["auc"], "after": v["auc"]}
            for g, v in ablated.items()
        },
    }

    # 3. Span-only view: TF-IDF trained and tested on the inserted spans only.
    sv = tfidf_vectorizer(cfg)
    xs = sv.fit_transform([inserted_text(r) for r in train])
    span_model = logreg(cfg).fit(xs, [r["label"] for r in train])
    span_scores = span_model.predict_proba(sv.transform([inserted_text(r) for r in test]))[:, 1]
    span_only = by_group(test, span_scores, fpr, _scenario)

    # 4. Words over-represented in benign spans vs injected spans: log-odds ratio with add-0.5
    #    smoothing, divided by its standard error (z), over all BIPIA rows.
    words = CountVectorizer(lowercase=True)
    ben = [inserted_text(r) for r in all_rows if r["label"] == 0]
    inj = [inserted_text(r) for r in all_rows if r["label"] == 1]
    xw = words.fit_transform(ben + inj)
    cb = np.asarray(xw[: len(ben)].sum(axis=0))[0] + 0.5
    ci = np.asarray(xw[len(ben) :].sum(axis=0))[0] + 0.5
    nb, ni = cb.sum(), ci.sum()
    z = (np.log(cb / (nb - cb)) - np.log(ci / (ni - ci))) / np.sqrt(1 / cb + 1 / ci)
    vocab = words.get_feature_names_out()
    top_words = [
        {
            "word": str(vocab[i]),
            "z": float(z[i]),
            "benign": int(cb[i] - 0.5),
            "injected": int(ci[i] - 0.5),
        }
        for i in np.argsort(-z, kind="stable")[:20]
    ]
    wset = {w["word"] for w in top_words}
    tok = words.build_analyzer()
    share_neg = sum(bool(wset & set(tok(t))) for t in ben) / len(ben)
    share_pos = sum(bool(wset & set(tok(t))) for t in inj) / len(inj)

    # 5. AUC by position and by attack group (TF-IDF on BIPIA test; a negative counts with its
    #    positive's attack group).
    scores = model.predict_proba(xte)[:, 1]
    by_pos = by_group(test, scores, fpr, lambda r: r.get("position"))
    group_of = {r["parent_id"]: r["attack_group"] for r in test if r["label"] == 1}
    by_attack = by_group(test, scores, fpr, lambda r: group_of.get(r["parent_id"]))
    return {
        "hard_negatives": hard_negative_view(cfg, test, scores, all_rows, style or {}),
        "top_features": top,
        "ablation": ablation,
        "span_only": span_only,
        "benign_words": {
            "top": top_words,
            "share_negatives": share_neg,
            "share_positives": share_pos,
        },
        "by_position": by_pos,
        "by_attack_group": dict(sorted(by_attack.items())),
    }


# ================================================================ report


def _t(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def _f(x: Any, pct: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    return f"{x:.1%}" if pct else f"{x:.3f}"


def _esc(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


OPTIONS = [
    "(a) choose the pool text per pair in P4 so that it avoids the over-used words, with no "
    "new API calls;",
    "(b) regenerate part of the P3 pool with more variety in style, including harmless texts "
    "that sound like instructions;",
    "(c) change a limit, with a written reason (team decision; P7 never edits a limit).",
]


def baseline_rows(cfg: Config, base: dict[str, Any]) -> list[list[Any]]:
    p7 = cfg.p7
    limit = {"length": p7.length_auc_stop, "tfidf": p7.tfidf_auc_stop}
    rows = []
    for name in ("length", "tfidf"):
        for ev, groups in base[name].items():
            for g, m in groups.items():
                applies = (ev == "bipia test" and g != "all") or (
                    ev == "nemotron test" and g == "all"
                )
                if not applies or m["auc"] is None:
                    status = "report"
                else:
                    status = "BLOCKED" if m["auc"] > limit[name] else "ok"
                rows.append(
                    [
                        name,
                        ev,
                        g,
                        f"{m['n']:,}",
                        _f(m["auc"]),
                        _f(m["recall"], True),
                        limit[name] if applies else "-",
                        status,
                    ]
                )
    return rows


def render(cfg: Config, version: str, res: dict[str, Any]) -> str:
    p7 = cfg.p7
    checks = res["checks"]
    scope = ", ".join(f"`{c}` ({', '.join(s)})" for c, s in SCOPE[version].items())
    L: list[str] = [
        f"# Report: p7_checks_{version}",
        "",
        f"Scope: {scope}. P7 only reads data/out and writes this report.",
        "",
    ]

    L += ["## Checks", ""]
    L.append(
        _t(
            ["part", "config", "check", "result", "detail", "examples"],
            [
                [
                    c["part"],
                    c["config"],
                    c["check"],
                    c["status"],
                    _esc(c["detail"]),
                    "; ".join(f"`{_esc(e)}`" for e in c["examples"]),
                ]
                for c in checks
            ]
            + [
                ["2", "both", "length gap", "report only", "see below", ""],
                [
                    "3",
                    "both",
                    "shortcut baselines",
                    "BLOCKED" if res["stop"] else "ok",
                    f"limits length {p7.length_auc_stop}, TF-IDF {p7.tfidf_auc_stop}",
                    "",
                ],
                ["3", "nemotron", "raw length AUC", "report only", "by construction", ""],
                ["4", "bipia", "diagnostics", "report only", "always run for TF-IDF", ""],
            ],
        )
    )
    L += [""]
    if res["evidence"]:
        L += [
            "**Check fixed (JSON newline case).** The first v0 run compared Nemotron injections "
            "with the raw JSON text, where a newline inside a string is the two characters `\\n`, "
            "and reported 899 false failures. The check now compares in decoded form (the "
            "document's JSON strings). The data was not changed. Three examples where the raw "
            "search fails and the decoded search finds the injection:",
            "",
            _t(
                ["sample_id", "raw match", "decoded match", "injection starts with"],
                [
                    [
                        e["sample_id"],
                        e["raw_match"],
                        e["decoded_match"],
                        f"`{_esc(e['injection_start'])}`",
                    ]
                    for e in res["evidence"]
                ],
            ),
            "",
        ]

    L += ["## Counts", ""]
    L.append(
        _t(
            ["config/split", "rows (loaded)", "rows (P4/P5 report)"],
            [
                [
                    k,
                    f"{v:,}",
                    f"{res['report_counts'][k]:,}" if k in res["report_counts"] else "n/a",
                ]
                for k, v in res["load_counts"].items()
            ],
        )
    )
    L += ["", "Position shares (BIPIA):", ""]
    L.append(
        _t(
            ["split", "scenario", "label", "rows", "start", "middle", "end"],
            [
                [
                    s["split"],
                    s["scenario"],
                    s["label"],
                    f"{s['rows']:,}",
                    *(_f(s[p], True) for p in POSITIONS),
                ]
                for s in res["position_shares"]
            ],
        )
    )

    L += ["", "## Length gap (positive minus negative, characters per pair; report only)", ""]
    L.append(
        _t(
            ["config", "split", "scenario", "pairs", "median", "5th pct", "95th pct"],
            [
                [
                    g["config"],
                    g["split"],
                    g["scenario"],
                    f"{g['pairs']:,}",
                    f"{g['median']:.0f}",
                    f"{g['p5']:.0f}",
                    f"{g['p95']:.0f}",
                ]
                for g in res["length_gaps"]
            ],
        )
    )
    L += [
        "",
        "Nemotron is large by design: the negative is the positive with the injection cut "
        "out. Not a failure.",
        "",
    ]

    L += [
        f"## Shortcut baselines (trained on BIPIA train only, seed {cfg.seed}; no detector)",
        "",
        "- Length: logistic regression on log(characters) and log(words) of the document. "
        f"Limit: AUC {p7.length_auc_stop}, per BIPIA scenario.",
        f"- TF-IDF: `TfidfVectorizer` fitted on the train documents only (word "
        f"{p7.tfidf.ngram[0]}-{p7.tfidf.ngram[1]}-grams, min_df {p7.tfidf.min_df}, sublinear_tf, "
        f"at most {p7.tfidf.max_features:,} features) + logistic regression (C = "
        f"{p7.tfidf.C}). Limit: AUC {p7.tfidf_auc_stop}, per BIPIA scenario and on Nemotron "
        "(pooled).",
        f"- Recall at {p7.fpr:.0%} FPR: threshold = the {100 * (1 - p7.fpr):.0f}th percentile "
        "of the negatives' scores; recall = share of positives scoring above it.",
        "- The limits are read from `configs/config.yaml` (the team's choice); P7 never edits "
        "them. A limit is exceeded when AUC > limit.",
        "",
    ]
    L.append(
        _t(
            [
                "baseline",
                "evaluated on",
                "scenario",
                "rows",
                "AUC",
                f"recall at {p7.fpr:.0%} FPR",
                "limit",
                "status",
            ],
            baseline_rows(cfg, res["baselines"]),
        )
    )
    L += [
        "",
        "**Nemotron length (report only).** Score = the raw document length in characters "
        "(longer = attack); AUC over the Nemotron test rows of each domain and pooled over all "
        "of them. *Positive longer* = share of pairs in which the positive's document is longer "
        "than its negative's. A BIPIA-trained length model carries no meaning on Nemotron, so "
        "none is reported.",
        "",
    ]
    L.append(
        _t(
            ["domain", "pairs", "AUC (raw length)", "positive longer"],
            [
                [g, v["pairs"], _f(v["auc"]), _f(v["positive_longer"], True)]
                for g, v in res["nemotron_length"].items()
            ],
        )
    )

    d = res["diagnostics"]
    L += [
        "",
        "## Diagnostics (TF-IDF on BIPIA)",
        "",
        "### 1. Strongest features per scenario and class",
        "",
        "Strength = weight x mean tf-idf in that scenario and class (test). Origin (letter "
        "after the feature): `i` = injection_span, `b` = benign_span (at least half of the "
        "train rows containing the feature have it inside their inserted span), `d` = the "
        "surrounding document.",
        "",
    ]
    for s, by in d["top_features"].items():
        for lab in ("1", "0"):
            feats = by[lab]
            orig = Counter(f["origin"] for f in feats)
            L += [
                f"**{s}, label {lab}** ({', '.join(f'{k} {v}' for k, v in sorted(orig.items()))}): "
                + ", ".join(f"`{f['feature']}` ({f['origin'][0]})" for f in feats),
                "",
            ]
    ab = d["ablation"]
    L += [
        "### 2. Ablation: without the 50 strongest negative-class features from benign_span",
        "",
        "Dropped: " + ", ".join(f"`{w}`" for w in ab["dropped"]),
        "",
    ]
    L.append(
        _t(
            ["scenario", "AUC before", "AUC after", "drop"],
            [
                [g, _f(v["before"]), _f(v["after"]), _f((v["before"] or 0) - (v["after"] or 0))]
                for g, v in ab["auc"].items()
            ],
        )
    )
    L += ["", "### 3. Span-only view (TF-IDF trained and tested on the inserted spans only)", ""]
    L.append(
        _t(
            ["scenario", "AUC", f"recall at {p7.fpr:.0%} FPR"],
            [[g, _f(m["auc"]), _f(m["recall"], True)] for g, m in d["span_only"].items()],
        )
    )
    bw = d["benign_words"]
    L += [
        "",
        "### 4. Words over-represented in the benign spans",
        "",
        "Log-odds ratio (benign spans vs injected spans, all BIPIA rows) with add-0.5 "
        "smoothing, divided by its standard error (z).",
        "",
    ]
    L.append(
        _t(
            ["word", "z", "count in benign spans", "count in injected spans"],
            [
                [w["word"], f"{w['z']:.1f}", f"{w['benign']:,}", f"{w['injected']:,}"]
                for w in bw["top"]
            ],
        )
    )
    L += [
        "",
        "Share of negatives whose benign span contains at least one of these 20 words: "
        f"**{bw['share_negatives']:.1%}** (injected spans: {bw['share_positives']:.1%}).",
        "",
        "### 5. AUC by position and by attack group (TF-IDF, BIPIA test)",
        "",
    ]
    L.append(
        _t(
            ["position", "rows", "AUC"],
            [[g, f"{m['n']:,}", _f(m["auc"])] for g, m in d["by_position"].items()],
        )
    )
    L += [""]
    L.append(
        _t(
            ["attack group", "rows", "AUC"],
            [[g, f"{m['n']:,}", _f(m["auc"])] for g, m in d["by_attack_group"].items()],
        )
    )
    hn = d.get("hard_negatives") or {}
    if hn:
        L += [
            "",
            "### 7. Hard negatives (P3 option B)",
            "",
            "Share of negatives whose benign text is a hard negative (a harmless text that "
            "sounds like an instruction to the human reader), and TF-IDF on BIPIA test with "
            "only hard-negative or only standard negatives (all positives kept).",
            "",
        ]
        L.append(
            _t(
                ["split", *SCEN_ORDER],
                [
                    [sp, *(_f(v.get(sc), True) for sc in SCEN_ORDER)]
                    for sp, v in hn["share"].items()
                ],
            )
        )
        L += [""]
        L.append(
            _t(
                ["negatives", "scenario", "AUC", f"recall at {p7.fpr:.0%} FPR"],
                [
                    [kind, g, _f(m["auc"]), _f(m["recall"], True)]
                    for kind, groups in hn["auc"].items()
                    for g, m in groups.items()
                ],
            )
        )
    L += ["", "### 6. Options (none applied)", ""] + [f"- {o}" for o in OPTIONS] + [""]
    groups_lt = cfg.bipia.allowed_shared_attack_groups
    if groups_lt:
        L += [
            "## Note on " + " / ".join(groups_lt),
            "",
            "Team decision: the attack names "
            + ", ".join(f"`{g}-0..4`" for g in groups_lt)
            + " stay in test although the same names exist in train (their strings differ). "
            "The attack_name check reports them as allowed. An evaluation without them could "
            "be interesting: the baselines are also reported on BIPIA test without "
            + " / ".join(groups_lt)
            + " (report only).",
            "",
        ]

    data_fail = [c for c in checks if c["status"] == "FAIL"]
    reasons = [f"data check failed: {c['config']}: {c['check']}" for c in data_fail]
    reasons += [f"limit exceeded: {h}" for h in res["stop"]]
    open_d = []
    if any("attack_name" in c["check"] for c in data_fail):
        open_d.append(
            "the attack names 'Language Translation-0..4' are in train and test "
            "(their strings differ; P1 kept them): decide whether that is allowed"
        )
    if res["stop"]:
        open_d.append("baseline limits exceeded: choose among the options above")
    L += ["## Result", ""]
    L.append(
        "PUSH ALLOWED"
        if not reasons
        else "PUSH BLOCKED: "
        + "; ".join(reasons)
        + (". Open decisions for the team: " + "; ".join(open_d) + "." if open_d else ".")
    )
    return "\n".join(L) + "\n"


def check_textfooler(cfg: Config, bipia_test: Rows, rows: Rows) -> list[dict]:
    """The `bipia_textfooler` test config (v2): every row traces to a test sample, only the
    inserted text changed, and only by word swaps within the caps."""
    from dataset_builder.steps.p6_span import check_variant, locate_span

    thr, cap = cfg.textfooler.threshold, cfg.textfooler.max_span_fraction
    parents = {r["sample_id"]: r for r in bipia_test}
    ids = [r["sample_id"] for r in rows]
    c = "bipia_textfooler"
    untraced = [r["sample_id"] for r in rows if r["parent_id"] not in parents]
    not_test = [r["sample_id"] for r in rows if r["split"] != "test" or r["source"] != c]
    dup = [i for i, n in Counter(ids).items() if n > 1] + [i for i in ids if i in parents]
    outside, rules, scores, labels = [], [], [], []
    for r in rows:
        par = parents.get(r["parent_id"])
        if par is None:
            continue
        method = r["generation_method"]
        same_doc = r["document"] == par["document"]
        if r["label"] != par["label"] or (method == "control") != same_doc:
            labels.append(r["sample_id"])
        malicious_ok = method != "textfooler" or r["label"] == 0 or (r["p_yes_base"] or 1) < thr
        if r["p_yes_base"] is None or not malicious_ok:
            scores.append(r["sample_id"])
        if method == "control":
            continue
        try:
            old = locate_span(par, None)
            if r["label"] == 1:
                new_ins = r["injection_span"]
            else:
                new_ins = r["document"][r["benign_span"][0] : r["benign_span"][1]]
            idx, old_ins = par["insert_index"], old.inserted()
            tail_ok = r["document"][idx + len(new_ins) :] == par["document"][idx + len(old_ins) :]
            if (
                r["document"][:idx] != par["document"][:idx]
                or not tail_ok
                or not new_ins.endswith(old.code)
            ):
                outside.append(r["sample_id"])
                continue
            toks = list(r["protected_tokens"] or [])
            toks += [t for t in (par.get("protected_tokens") or []) if t not in toks]
            reason, _, _ = check_variant(old, new_ins[: len(new_ins) - len(old.code)], toks, cap)
            if reason:
                rules.append(f"{r['sample_id']}: {reason}")
        except (KeyError, TypeError, ValueError) as e:  # a span that cannot be found
            outside.append(f"{r['sample_id']}: {e}")
    by_method = Counter((r["generation_method"], r["label"]) for r in rows)
    n_ctl, n_pert = by_method[("control", 0)], by_method[("textfooler", 0)]
    return [
        result("1", "textfooler: every parent_id is a bipia test sample", c, untraced),
        result("1", "textfooler: split is test, source is bipia_textfooler", c, not_test),
        result("1", "textfooler: sample_ids unique and not in bipia", c, dup),
        result("1", "textfooler: label kept; controls unchanged, variants changed", c, labels),
        result("1", "textfooler: only the inserted text changed", c, outside),
        result("1", "textfooler: word swaps only, within the cap, protected tokens kept", c, rules),
        result("1", "textfooler: p_yes_base set; malicious variants below threshold", c, scores),
        result(
            "1",
            "textfooler: as many plain twins as perturbed twins",
            c,
            [] if n_ctl == n_pert else [f"{n_ctl} control vs {n_pert} perturbed"],
            f"{len(rows):,} rows: {dict(by_method)}",
        ),
    ]


# ================================================================ entry point


def load(cfg: Config, config: str, splits: Iterable[str]) -> dict[str, Rows]:
    return {s: pq.read_table(cfg.paths.out / config / f"{s}.parquet").to_pylist() for s in splits}


def report_counts(cfg: Config, version: str = "v0") -> dict[str, dict[str, int]]:
    p4 = json.loads(report_path(cfg, "p4_composition.json").read_text(encoding="utf-8"))
    p5 = json.loads(report_path(cfg, "p5_nemotron.json").read_text(encoding="utf-8"))
    out = {
        "bipia": {k: int(v) for k, v in p4["split"].items()},
        "nemotron": {k: int(v) for k, v in p5["split"].items()},
    }
    if "bipia_textfooler" in SCOPE[version]:
        p6 = json.loads(report_path(cfg, "p6_textfooler.json").read_text(encoding="utf-8"))
        out["bipia_textfooler"] = {k: int(v) for k, v in p6["split"].items()}
    return out


def nemotron_goals(cfg: Config) -> dict[int, str]:
    from dataset_builder.nemotron_source import load_records, raw_dir

    path = raw_dir(cfg) / cfg.nemotron.filename
    return {r["id"]: r["goal"] for r in load_records(path)} if path.is_file() else {}


def run(cfg: Config | None = None, version: str = "v1") -> dict[str, Any]:
    cfg = cfg or load_config()
    if version not in SCOPE:
        raise P7NotReadyError(
            f"P7 {version}: {LATER.get(version, 'unknown version')}; "
            "only v0, v1 and v2 can run now."
        )
    scope = SCOPE[version]
    bipia = load(cfg, "bipia", scope["bipia"])
    nem = load(cfg, "nemotron", scope["nemotron"])
    nemotron = [r for rs in nem.values() for r in rs]
    attacks = list(read_jsonl(cfg.paths.interim / "attacks.jsonl"))
    docs = list(read_jsonl(cfg.paths.interim / "bipia_docs.jsonl"))
    ref = report_counts(cfg, version)

    checks, load_counts = check_load(cfg, scope, ref)
    checks += check_splits(bipia) + check_attacks_in_test(
        bipia, attacks, cfg.bipia.allowed_shared_attack_groups
    )
    pos_checks, evidence = check_positives(bipia, nemotron)
    checks += pos_checks + check_negatives(bipia, attacks, nemotron, nemotron_goals(cfg))
    bal, shares = check_balance(cfg, "bipia", bipia)
    checks += bal + check_balance(cfg, "nemotron", nem)[0]
    checks += check_pairs(bipia, docs) + check_queries(cfg, bipia, docs)
    if "bipia_textfooler" in scope:
        tf_rows = load(cfg, "bipia_textfooler", scope["bipia_textfooler"])["test"]
        checks += check_textfooler(cfg, bipia["test"], tf_rows)
    else:
        checks.append(
            result(
                "1", "TextFooler checks", "bipia_textfooler", None, f"not in {version} (from v2)"
            )
        )

    gaps = length_gaps("bipia", bipia) + length_gaps("nemotron", nem)
    base = baselines(cfg, bipia["train"], bipia["test"], nemotron)
    pool = cfg.paths.interim / "benign_pool.jsonl"
    style = (
        {b["benign_id"]: b.get("style", "standard") for b in read_jsonl(pool)}
        if pool.is_file()
        else {}
    )
    diag = diagnostics(
        cfg, bipia["train"], bipia["test"], base, [r for rs in bipia.values() for r in rs], style
    )
    base.pop("_models")
    stop = stop_rule(cfg, base)
    data_ok = all(c["status"] != "FAIL" for c in checks)
    res = {
        "version": version,
        "checks": checks,
        "evidence": evidence,
        "load_counts": load_counts,
        "report_counts": {f"{c}/{s}": n for c, v in ref.items() for s, n in v.items()},
        "position_shares": shares,
        "length_gaps": gaps,
        "baselines": base,
        "nemotron_length": nemotron_length(nemotron),
        "diagnostics": diag,
        "stop": stop,
        "limits": {
            "length_auc_stop": cfg.p7.length_auc_stop,
            "tfidf_auc_stop": cfg.p7.tfidf_auc_stop,
        },
        "status": "PUSH ALLOWED" if data_ok and not stop else "PUSH BLOCKED",
        "exit_code": EXIT_OK if data_ok and not stop else (EXIT_LIMIT if data_ok else EXIT_DATA),
    }
    name = f"p7_checks_{version}"
    report_path(cfg, f"{name}.json").write_text(
        json.dumps(res, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    md = report_path(cfg, f"{name}.md")
    md.write_text(render(cfg, version, res), encoding="utf-8")
    return res | {"report": md}
