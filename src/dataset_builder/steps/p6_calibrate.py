"""P6 judge calibration: is the local judge good enough to filter TextFooler variants?

Set A (no hand labelling): about 100 original spans with a known label (the dataset's) from test
pairs, spread over scenarios and both classes. The judge gets the original as both texts; it must
say `label_still_correct`. Score: accuracy per class (suggested floor 95%).

Set B (hand-labelled once): about 100 modified texts. Half are real TextFooler variants from the
pilot, half are machine-made bad variants (the text cut in half, or its words shuffled). The step
writes a template CSV; a person fills `human_same_meaning` and `human_label_correct` (true/false);
run it again with `--set-b <csv>` to score the judge: agreement with the person on both fields and
the false-accept rate (a variant the person calls bad that the judge accepts).

Needs the local judge server; no GPU work beyond that. Writes reports/p6/p6_judge_calibration.md.
"""

import csv
import logging
import random
import time
from pathlib import Path
from typing import Any

from dataset_builder.config import Config, load_config
from dataset_builder.io import read_jsonl, read_parquet, rng
from dataset_builder.reports import report_path
from dataset_builder.steps.p6_judge import Judge, build_judge
from dataset_builder.steps.p6_sampling import SCENARIOS, make_pairs
from dataset_builder.steps.p6_span import locate_span

logger = logging.getLogger(__name__)

SET_B_COLUMNS = (
    "id",
    "scenario",
    "label",
    "origin",
    "original",
    "modified",
    "human_same_meaning",
    "human_label_correct",
)
TRUE = {"true", "yes", "1", "y"}
FALSE = {"false", "no", "0", "n"}


def set_a(cfg: Config, rows: list[dict[str, Any]], n_per_class: int = 50) -> list[dict[str, Any]]:
    """Original spans with the dataset's label, spread over scenarios (positives and negatives)."""
    pairs = make_pairs(rows)
    r = rng("p6:calibrate:a", cfg)
    out: list[dict[str, Any]] = []
    for label, field in ((1, "pos"), (0, "neg")):
        by_s = {s: [p for p in pairs if p["scenario"] == s] for s in SCENARIOS}
        for s in by_s:
            by_s[s].sort(key=lambda p: p["pair_id"])
            r.shuffle(by_s[s])
        picked: list[dict[str, Any]] = []
        i = 0
        while len(picked) < n_per_class and any(i < len(v) for v in by_s.values()):
            picked += [by_s[s][i][field] for s in SCENARIOS if i < len(by_s[s])][
                : n_per_class - len(picked)
            ]
            i += 1
        for row in picked:
            span = locate_span(row, None)
            out.append(
                {
                    "id": row["sample_id"],
                    "scenario": row["scenario"],
                    "label": label,
                    "text": span.inserted(),
                }
            )
    return out


def corrupt(text: str, how: str, r: random.Random) -> str:
    words = text.split()
    if how == "cut":
        return " ".join(words[: max(len(words) // 2, 1)])
    shuffled = words[:]
    r.shuffle(shuffled)
    return " ".join(shuffled)


def write_set_b_template(
    cfg: Config, rows: list[dict[str, Any]], pilot_results: list[dict[str, Any]], n: int = 100
) -> Path:
    """Real variants from the pilot results plus machine-made bad variants, to be hand-labelled."""
    r = rng("p6:calibrate:b", cfg)
    real = [
        x for x in pilot_results if x.get("new_span") and x["status"] in ("kept", "judge_rejected")
    ]
    real.sort(key=lambda x: x["key"])
    r.shuffle(real)
    items = [
        {
            "origin": "textfooler",
            "scenario": x["scenario"],
            "label": x["label"],
            "original": x["inserted"],
            "modified": x["new_span"] + x["inserted"][len(x["span"]) :],
        }
        for x in real[: n // 2]
    ]
    pairs = make_pairs(rows)
    pairs.sort(key=lambda p: p["pair_id"])
    r.shuffle(pairs)
    for i, p in enumerate(pairs[: n - len(items)]):
        row = p["pos"] if i % 2 == 0 else p["neg"]
        text = locate_span(row, None).inserted()
        how = "cut" if i % 4 < 2 else "shuffle"
        items.append(
            {
                "origin": f"machine-{how}",
                "scenario": row["scenario"],
                "label": int(row["label"]),
                "original": text,
                "modified": corrupt(text, how, r),
            }
        )
    path = report_path(cfg, "p6_judge_calibration_set_b.csv").with_name(
        "judge_calibration_set_b.csv"
    )
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SET_B_COLUMNS)
        w.writeheader()
        for i, it in enumerate(items):
            w.writerow(
                {"id": f"b{i:03d}", **it, "human_same_meaning": "", "human_label_correct": ""}
            )
    return path


def _flag(s: str) -> bool | None:
    s = s.strip().lower()
    return True if s in TRUE else False if s in FALSE else None


def score_set_a(judge: Judge, items: list[dict[str, Any]]) -> dict[str, Any]:
    t0 = time.monotonic()
    vs = judge.verdicts([(i["text"], i["text"], i["label"], i["scenario"]) for i in items])
    secs = time.monotonic() - t0
    out: dict[str, Any] = {
        "n": len(items),
        "seconds_per_item": secs / max(len(items), 1),
        "unusable": sum(v is None for v in vs),
    }
    for label in (1, 0):
        ok = [
            bool(v and v.label_still_correct)
            for i, v in zip(items, vs, strict=True)
            if i["label"] == label
        ]
        out[f"accuracy_{label}"] = sum(ok) / len(ok) if ok else None
    by_s = {}
    for s in SCENARIOS:
        ok = [
            bool(v and v.label_still_correct)
            for i, v in zip(items, vs, strict=True)
            if i["scenario"] == s
        ]
        by_s[s] = sum(ok) / len(ok) if ok else None
    out["accuracy_by_scenario"] = by_s
    return out


def score_set_b(judge: Judge, path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    rows = [
        r
        for r in rows
        if _flag(r["human_same_meaning"]) is not None
        and _flag(r["human_label_correct"]) is not None
    ]
    vs = judge.verdicts(
        [(r["original"], r["modified"], int(r["label"]), r["scenario"]) for r in rows]
    )
    agree_m, agree_l, bad, bad_accepted = [], [], 0, 0
    for r, v in zip(rows, vs, strict=True):
        hm, hl = _flag(r["human_same_meaning"]), _flag(r["human_label_correct"])
        agree_m.append(v is not None and v.same_meaning == hm)
        agree_l.append(v is not None and v.label_still_correct == hl)
        if not (hm and hl):  # the person calls it a bad variant
            bad += 1
            bad_accepted += bool(v and v.accepted)
    n = len(rows)
    return {
        "n": n,
        "agreement_same_meaning": sum(agree_m) / n if n else None,
        "agreement_label_still_correct": sum(agree_l) / n if n else None,
        "bad_variants": bad,
        "false_accept_rate": bad_accepted / bad if bad else None,
    }


def run(
    cfg: Config | None = None, *, set_b: Path | None = None, judge: Judge | None = None
) -> dict[str, Any]:
    cfg = cfg or load_config()
    judge = judge or build_judge(cfg.textfooler.judge)
    rows = read_parquet(cfg.paths.out / "bipia" / "test.parquet")
    a = score_set_a(judge, set_a(cfg, rows))
    lines = [
        "# P6 judge calibration",
        "",
        f"Judge: `{judge.name}` (local). Set A needs no hand labelling; set B needs a person.",
        "",
        "## Set A: label on original spans",
        "",
        f"- Items: {a['n']}; unusable answers: {a['unusable']}; "
        f"seconds per item: {a['seconds_per_item']:.1f}",
        f"- Accuracy on malicious originals: {a['accuracy_1']:.1%}"
        if a["accuracy_1"] is not None
        else "- Accuracy on malicious originals: n/a",
        f"- Accuracy on harmless originals: {a['accuracy_0']:.1%}"
        if a["accuracy_0"] is not None
        else "- Accuracy on harmless originals: n/a",
        f"- By scenario: {a['accuracy_by_scenario']}",
        "- Suggested floor: 95% per class (the team sets it).",
        "",
    ]
    result: dict[str, Any] = {"set_a": a}
    if set_b is None:
        pilot = cfg.paths.interim / "p6_results_pilot.jsonl"
        pilot_results = list(read_jsonl(pilot)) if pilot.is_file() else []
        template = write_set_b_template(cfg, rows, pilot_results)
        lines += [
            "## Set B",
            "",
            f"Template written to `{template}`. A person fills `human_same_meaning` and "
            "`human_label_correct` (true/false) and the step is run again with `--set-b`.",
            "",
        ]
        result["set_b_template"] = template
    else:
        b = score_set_b(judge, set_b)
        result["set_b"] = b
        lines += [
            "## Set B: modified texts, against a person",
            "",
            f"- Items: {b['n']}; bad variants (by the person): {b['bad_variants']}",
            f"- Agreement on same_meaning: {b['agreement_same_meaning']}",
            f"- Agreement on label_still_correct: {b['agreement_label_still_correct']}",
            "- False-accept rate (a bad variant the judge accepts): "
            f"{b['false_accept_rate']} (suggested at most 5%)",
            "",
        ]
    lines += ["## Operating checks", "", f"- {judge.info()}", ""]
    path = report_path(cfg, "p6_judge_calibration.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    result["report"] = path
    return result
