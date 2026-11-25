"""P6 reports: statistics, the pilot projection, the full report and the human-check CSV.

Everything here is plain Python over the records of p6_results.jsonl, so it is tested without a
GPU. Confidence intervals resample documents, not examples (examples of one document are not
independent).
"""

import csv
import random
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dataset_builder.config import Config
from dataset_builder.io import rng
from dataset_builder.reports import report_path, write_counts_report
from dataset_builder.schema import TextFoolerSample

SCENARIOS = ("email", "table", "code")
SUCCEEDED = ("rule_rejected", "judge_rejected", "kept")


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _pct(a: float, b: float) -> str:
    return f"{a / b:.1%}" if b else "n/a"


def _esc(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ⏎ ")


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def doc_bootstrap(
    values: Sequence[tuple[str, float]], seed: int, n: int = 1000
) -> tuple[float, float, float] | None:
    """(mean, low, high): the mean of `values` (document, value) with a 95% interval from
    resampling documents with replacement."""
    if not values:
        return None
    by_doc: dict[str, list[float]] = {}
    for d, v in values:
        by_doc.setdefault(d, []).append(v)
    docs = sorted(by_doc)
    sums = {d: (sum(by_doc[d]), len(by_doc[d])) for d in docs}
    r = random.Random(seed)
    means = []
    for _ in range(n):
        picks = [sums[r.choice(docs)] for _ in docs]
        tot = sum(c for _, c in picks)
        means.append(sum(s for s, _ in picks) / tot)
    means.sort()
    mean = sum(v for _, v in values) / len(values)
    return mean, means[int(0.025 * (n - 1))], means[int(0.975 * (n - 1))]


def _ci(ci: tuple[float, float, float] | None) -> str:
    return "n/a" if ci is None else f"{ci[0]:.1%} [{ci[1]:.1%}, {ci[2]:.1%}]"


def summarise(rs: list[dict[str, Any]]) -> dict[str, Any]:
    """Outcome counts and rates of a set of candidates (one scenario and kind)."""
    c = Counter(r["status"] for r in rs)
    succeeded = [r for r in rs if r["status"] in SUCCEEDED]
    attacked = succeeded + [r for r in rs if r["status"] == "failed"]
    kept = [r for r in rs if r["status"] == "kept"]
    judged = c.get("kept", 0) + c.get("judge_rejected", 0)
    return {
        "processed": len(rs),
        "skipped": c.get("skipped", 0),
        "judge_disagrees": c.get("judge_disagrees", 0),
        "no_editable": c.get("no_editable", 0),
        "failed": c.get("failed", 0),
        "rule_rejected": c.get("rule_rejected", 0),
        "judge_rejected": c.get("judge_rejected", 0),
        "succeeded": len(succeeded),
        "kept": len(kept),
        "attack_success_rate": len(succeeded) / len(attacked) if attacked else None,
        "judge_rejection_rate": c.get("judge_rejected", 0) / judged if judged else None,
        "words_changed_mean": _mean([r["words_changed"] for r in kept]),
        "words_changed_pct_mean": _mean(
            [r["words_changed"] / r["n_words"] for r in kept if r["n_words"]]
        ),
        "queries_per_success": _mean([r["queries"] for r in succeeded]),
        "seconds_per_candidate": _mean([r["seconds"] for r in rs]),
        "keep_rate": len(kept) / len(rs) if rs else None,
        "flipped": sum(1 for r in kept if r.get("flipped")),
    }


def stats_of(results: list[dict[str, Any]]) -> dict[str, Any]:
    """by_bucket["scenario/1"] = malicious candidates, ["scenario/0"] = perturbed twins."""
    out: dict[str, Any] = {"by_bucket": {}, "all": summarise(results)}
    for s in SCENARIOS:
        for kind, lab in (("malicious", 1), ("twin", 0)):
            rs = [r for r in results if r["scenario"] == s and r["kind"] == kind]
            if rs:
                out["by_bucket"][f"{s}/{lab}"] = summarise(rs)
    return out


def projection(results: list[dict[str, Any]], cfg: Config) -> dict[str, Any]:
    """From the pilot: attempts and hours the full run needs, per scenario.

    Malicious attempts = target / keep rate. A twin costs its own time; replacements follow the
    twin keep rate. A scenario with no kept variant in the pilot has no estimate."""
    tf = cfg.textfooler
    rows, total = [], 0.0
    for s in SCENARIOS:
        mal = summarise([r for r in results if r["scenario"] == s and r["kind"] == "malicious"])
        twin = summarise([r for r in results if r["scenario"] == s and r["kind"] == "twin"])
        target = tf.targets.get(s, 0)
        if mal["keep_rate"]:
            attempts = target / mal["keep_rate"]
            hours = attempts * mal["seconds_per_candidate"]
            if twin["keep_rate"]:
                hours += target / twin["keep_rate"] * twin["seconds_per_candidate"]
            hours /= 3600
            total += hours
        else:
            attempts = hours = None
        rows.append(
            {
                "scenario": s,
                "target": target,
                "keep_rate": mal["keep_rate"],
                "attempts": attempts,
                "hours": hours,
                "twin_keep_rate": twin["keep_rate"],
            }
        )
    unknown = any(r["hours"] is None for r in rows)
    return {
        "rows": rows,
        "total_hours": None if unknown else total,
        "unreasonable": unknown or total > tf.max_projected_hours,
    }


def _run_section(cfg: Config, info: dict[str, Any], wall: float, mode: str) -> tuple[str, str]:
    tf = cfg.textfooler
    c = info.get("constraints", {})
    lines = [
        f"- Mode: {mode}; seed {cfg.seed}.",
        f"- Victim: `{info.get('victim')}` ({info.get('victim_dtype')}); "
        "input = instruct + query + "
        "the whole document; score = p(yes) from the two answer-token logits "
        f"(token ids {info.get('answer_token_ids')}); threshold {tf.threshold}.",
        f"- Judge: `{info.get('judge')}` (revision {info.get('judge_revision') or 'not recorded'}, "
        f"reasoning effort {info.get('reasoning_effort')}), local; "
        f"{info.get('judge_calls', 0)} calls, "
        f"{info.get('judge_unparsed', 0)} unusable answers.",
        "- Versions: " + ", ".join(f"{k} {v}" for k, v in info.get("versions", {}).items()),
        f"- Victim documents scored: {info.get('documents_scored', 0):,} of "
        f"{info.get('documents_requested', 0):,} requested (the rest from the cache).",
        f"- Wall time: {wall / 60:.1f} min.",
        "",
        "**TextFooler constraint values**",
        "",
        _table(
            ["setting", "value"],
            [
                ["recipe", "TextAttack `TextFoolerJin2019` (MIT)"],
                [
                    "transformation",
                    f"{c.get('transformation')}, {c.get('max_candidates')} synonym "
                    "candidates per word",
                ],
                ["word-embedding cosine similarity", f">= {c.get('min_cos_sim')}"],
                [
                    "sentence similarity (USE, angular)",
                    f">= {c.get('use_threshold')}, window {c.get('use_window')}",
                ],
                [
                    "maximum fraction of words perturbed",
                    f"{c.get('max_span_fraction', tf.max_span_fraction):.0%} (ours)",
                ],
                ["pre-transformation constraints", ", ".join(c.get("pre_transformation", []))],
                ["constraints", ", ".join(c.get("constraints", []))],
                ["search / goal", f"{c.get('search')} / {c.get('goal')}"],
            ],
        ),
    ]
    return "Run", "\n".join(lines)


def _outcomes_section(stats: dict[str, Any]) -> tuple[str, str]:
    rows = []
    for key, st in stats["by_bucket"].items():
        rows.append(
            [
                key,
                "malicious" if key.endswith("/1") else "perturbed twin",
                st["processed"],
                st["skipped"],
                st["judge_disagrees"],
                st["no_editable"],
                st["failed"],
                _pct(st["succeeded"], st["succeeded"] + st["failed"]),
                st["rule_rejected"],
                st["judge_rejected"],
                _pct(st["judge_rejected"], st["judge_rejected"] + st["kept"]),
                f"{st['words_changed_mean']:.2f} ({st['words_changed_pct_mean']:.1%})",
                f"{st['queries_per_success']:.0f}",
                f"{st['seconds_per_candidate']:.1f}",
                st["kept"],
            ]
        )
    head = [
        "scenario/class",
        "kind",
        "attempts",
        "skipped originals",
        "judge disagrees",
        "no editable word",
        "failed",
        "attack success rate",
        "rule rejected",
        "judge rejected",
        "judge rejection rate",
        "words changed (% of span)",
        "queries per success",
        "seconds per attempt",
        "kept",
    ]
    note = (
        "\n\n*Attack success rate* = a change reached the goal / (reached + failed). For a "
        "perturbed twin the goal is a positive prediction, but a twin is kept whether or not it "
        "flipped. *Skipped originals* are malicious originals the victim already misses. *Judge "
        "disagrees* = the judge's verdict on the original differs from its label."
    )
    return "Outcomes per scenario", _table(head, rows) + note


def detection_section(
    cfg: Config,
    results: list[dict[str, Any]],
    controls: list[tuple[str, float]],
    samples: list[TextFoolerSample] | None,
) -> tuple[str, str]:
    thr, seed = cfg.textfooler.threshold, cfg.seed
    rows = []
    for s in (*SCENARIOS, "all"):
        pick = lambda r, s=s: s == "all" or r["scenario"] == s  # noqa: E731
        mal = [
            (r["doc"], float(r["p_orig"] >= thr))
            for r in results
            if r["kind"] == "malicious" and "p_orig" in r and pick(r)
        ]
        twin_kept = [
            r for r in results if r["kind"] == "twin" and r["status"] == "kept" and pick(r)
        ]
        flip = [(r["doc"], float(bool(r.get("flipped")))) for r in twin_kept]
        ctl = [(d, float(p >= thr)) for d, p in controls] if s == "all" else None
        rows.append(
            [
                s,
                _ci(doc_bootstrap(mal, seed)),
                "0% by construction",
                _ci(doc_bootstrap(ctl, seed)) if ctl is not None else "see all",
                _ci(doc_bootstrap(flip, seed)),
            ]
        )
    body = _table(
        [
            "scenario",
            "detection on originals (attacked sample)",
            "detection on kept variants",
            "false-positive rate, plain benign (control)",
            "false-positive rate, perturbed benign (flip rate)",
        ],
        rows,
    )
    note = (
        "\n\nReported separately, with 95% intervals from resampling **documents**. Detection on "
        "originals counts the malicious originals that were scored (not skipped by the judge "
        "check). Kept variants are fooling by construction. The control rate is over all "
        "scenarios together (the controls are the kept positives' twins, copied unchanged)."
    )
    return "Detection and false-positive rates", body + note


def pilot_projection_section(proj: dict[str, Any], cfg: Config) -> tuple[str, str]:
    rows = [
        [
            r["scenario"],
            r["target"],
            f"{r['keep_rate']:.1%}" if r["keep_rate"] else "n/a",
            f"{r['attempts']:.0f}" if r["attempts"] is not None else "n/a",
            f"{r['hours']:.1f}" if r["hours"] is not None else "n/a",
            f"{r['twin_keep_rate']:.1%}" if r["twin_keep_rate"] else "n/a",
        ]
        for r in proj["rows"]
    ]
    total = (
        f"**{proj['total_hours']:.1f} h**"
        if proj["total_hours"] is not None
        else "**unknown** (a scenario had no kept variant)"
    )
    verdict = (
        f"**Unreasonable** (above {cfg.textfooler.max_projected_hours:.0f} h, or unknown): "
        "stop and "
        "revisit the constraints, the target or the victim before the full run."
        if proj["unreasonable"]
        else "Within the limit: the full run can start."
    )
    body = _table(
        [
            "scenario",
            "target kept",
            "keep rate (pilot)",
            "pairs to attempt",
            "hours",
            "twin keep rate",
        ],
        rows,
    )
    return (
        "Projected full run",
        f"{body}\n\nProjected total: {total}. {verdict} The attempt cap (`attempt_cap`) is "
        "set from the pairs column.",
    )


def examples_section(results: list[dict[str, Any]], kind: str, n: int = 10) -> tuple[str, str]:
    ex = [r for r in results if r["kind"] == kind and r.get("new_span")][:n]
    lines = []
    for r in ex:
        old_w, new_w = WORD_SPLIT(r["span"]), WORD_SPLIT(r["new_span"])
        swaps = [f"{a} → {b}" for a, b in zip(old_w, new_w, strict=False) if a != b]
        lines += [
            f"**{r['scenario']} / {kind} / {r['status']}** "
            f"(p(yes) {r.get('p_orig', 0):.2f} → {r.get('p_new', 0):.2f}, "
            f"{r.get('queries')} queries)",
            "",
            f"- before: {_esc(r['span'])}",
            f"- after: {_esc(r['new_span'])}",
            f"- swaps: {', '.join(swaps) or 'none'}",
            "",
        ]
    return f"Examples ({kind})", "\n".join(lines) or "none"


def _attempted(results: list[dict[str, Any]], scenario: str) -> int:
    return sum(1 for r in results if r["kind"] == "malicious" and r["scenario"] == scenario)


def WORD_SPLIT(text: str) -> list[str]:
    from dataset_builder.steps.p6_span import WORD

    return WORD.findall(text)


NOTES_PLACEHOLDER = (
    "Read 20 kept malicious variants and 20 perturbed benign variants in full "
    "(`reports/p6/human_check.csv` and the examples above) and write the notes here: "
    "meaning kept, label kept, odd swaps, anything the rules missed."
)


def write_pilot(
    cfg: Config, results: list[dict[str, Any]], engine: Any, judge: Any, wall: float
) -> dict[str, Any]:
    stats = stats_of(results)
    proj = projection(results, cfg)
    info = engine.info() | judge.info()
    sections = [
        _run_section(
            cfg,
            info,
            wall,
            f"pilot ({len({r['pair_id'] for r in results})} pairs, nothing written to data/out)",
        ),
        _outcomes_section(stats),
        detection_section(cfg, results, [], None),
        pilot_projection_section(proj, cfg),
        examples_section(results, "malicious"),
        examples_section(results, "twin"),
    ]
    write_counts_report(
        "p6_textfooler",
        [{"scenario": r["scenario"], "kind": r["kind"], "status": r["status"]} for r in results],
        keys=("scenario", "kind", "status"),
        cfg=cfg,
        sections=sections,
        extra={"mode": "pilot", "stats": stats, "info": info, "projection": proj},
        name="p6_textfooler_pilot",
    )
    return {
        "report": report_path(cfg, "p6_textfooler_pilot.md"),
        "stats": stats,
        "projection": proj,
    }


def write_full(
    cfg: Config,
    samples: list[TextFoolerSample],
    results: list[dict[str, Any]],
    counts: dict[str, Any],
    info: dict[str, Any],
    orders: dict[str, list[dict[str, Any]]],
    wall: float,
) -> Path:
    tf = cfg.textfooler
    main = [r for r in results if r["attack_group"] != tf.translation_group or r["kind"] == "twin"]
    translation = [
        r for r in results if r["kind"] == "malicious" and r["attack_group"] == tf.translation_group
    ]
    stats = stats_of(main)
    kept_by = Counter(
        r["scenario"] for r in main if r["kind"] == "malicious" and r["status"] == "kept"
    )
    short = {s: kept_by[s] for s in SCENARIOS if kept_by[s] < tf.floor_per_scenario}
    shortfall = (
        "all scenarios reached the floor of " + str(tf.floor_per_scenario)
        if not short
        else f"BELOW the floor of {tf.floor_per_scenario}: {short}. "
        "Explain: candidates or the attempt cap ran out."
    )
    controls = [tuple(x) for x in counts.get("control_p", [])]
    sections = [
        _run_section(cfg, info, wall, "full"),
        _outcomes_section(stats),
        (
            "Targets",
            f"- Kept malicious variants (main set): {sum(kept_by.values())} "
            f"(email {kept_by['email']}, table {kept_by['table']}, "
            f"code {kept_by['code']}); {shortfall}.\n"
            f"- Plain twins (control): {counts['control']}; "
            f"perturbed twins kept: {counts['perturbed_twins']}.\n"
            f"- Pairs attempted per scenario: "
            + ", ".join(f"{s} {_attempted(main, s)}/{len(orders[s])}" for s in SCENARIOS),
        ),
        detection_section(cfg, main, controls, samples),
        examples_section(main, "malicious"),
        examples_section(main, "twin"),
        ("Reading notes (20 + 20 variants)", NOTES_PLACEHOLDER),
        (
            "Judge agreement with a person",
            "Fill the `human_verdict` column of `reports/p6/human_check.csv` (accept or reject), "
            "then run "
            "`dataset-builder p6-agreement`. The team sets the minimum; 90% is the suggestion.",
        ),
    ]
    if translation:
        sections.insert(
            3,
            (
                "Translation slice (separate, optional)",
                _table(
                    ["scenario", "attempts", "kept", "attack success rate"],
                    [
                        [
                            s,
                            st["processed"],
                            st["kept"],
                            _pct(st["succeeded"], st["succeeded"] + st["failed"]),
                        ]
                        for s in SCENARIOS
                        if (st := summarise([r for r in translation if r["scenario"] == s]))[
                            "processed"
                        ]
                    ],
                )
                + f"\n\nGroup `{tf.translation_group}`. "
                "Not counted in the targets or the floors above.",
            ),
        )
    write_counts_report(
        "p6_textfooler",
        samples,
        keys=("scenario", "split", "label", "position", "generation_method"),
        cfg=cfg,
        sections=sections,
        extra={
            "mode": "full",
            "stats": stats,
            "info": info,
            "translation": summarise(translation) if translation else None,
            "twins": {k: v for k, v in counts.items() if k != "control_p"},
            "wall_seconds": wall,
        },
        name="p6_textfooler",
    )
    return report_path(cfg, "p6_textfooler.md")


# ---------------------------------------------------------------- human check


HUMAN_COLUMNS = (
    "key",
    "scenario",
    "class",
    "original",
    "variant",
    "judge_same_meaning",
    "judge_label_still_correct",
    "judge_accepted",
    "judge_reason",
    "human_verdict",
)


def write_human_check(
    cfg: Config, results: list[dict[str, Any]], sources: dict[str, dict[str, Any]]
) -> Path:
    """`human_check_n` judged variants spread over scenario and class (kept and judge-rejected, so
    agreement measures both kinds of error), shuffled with the seed. `human_verdict` is empty."""
    judged = [r for r in results if r.get("verdict") and r["status"] in ("kept", "judge_rejected")]
    strata: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in sorted(judged, key=lambda r: r["key"]):
        strata.setdefault((r["scenario"], r["label"]), []).append(r)
    r_ = rng("p6:human_check", cfg)
    for items in strata.values():
        r_.shuffle(items)
    keys = sorted(strata)
    picked: list[dict[str, Any]] = []
    i = 0
    n = cfg.textfooler.human_check_n
    while len(picked) < n and any(i < len(strata[k]) for k in keys):
        picked += [strata[k][i] for k in keys if i < len(strata[k])][: n - len(picked)]
        i += 1
    path = report_path(cfg, "p6_human_check.csv").with_name("human_check.csv")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HUMAN_COLUMNS)
        w.writeheader()
        for r in picked:
            v = r["verdict"]
            w.writerow(
                {
                    "key": r["key"],
                    "scenario": r["scenario"],
                    "class": "malicious" if r["label"] == 1 else "benign",
                    "original": r["inserted"],
                    "variant": r["new_span"] + r["inserted"][len(r["span"]) :],
                    "judge_same_meaning": v["same_meaning"],
                    "judge_label_still_correct": v["label_still_correct"],
                    "judge_accepted": r["status"] == "kept",
                    "judge_reason": v["reason"],
                    "human_verdict": "",
                }
            )
    return path


_TRUE = {"accept", "yes", "y", "1", "true", "ok"}
_FALSE = {"reject", "no", "n", "0", "false"}


def human_agreement(path: Path) -> dict[str, Any]:
    """Agreement between the person's accept/reject and the judge's, over the filled rows."""
    with Path(path).open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    done = [r for r in rows if r["human_verdict"].strip().lower() in _TRUE | _FALSE]
    if not done:
        return {"filled": 0, "of": len(rows), "agreement": None}

    def agree(rs: list[dict[str, str]]) -> float | None:
        ok = [
            (r["human_verdict"].strip().lower() in _TRUE) == (r["judge_accepted"] == "True")
            for r in rs
        ]
        return sum(ok) / len(ok) if ok else None

    out: dict[str, Any] = {
        "filled": len(done),
        "of": len(rows),
        "agreement": agree(done),
        "by_class": {},
        "by_scenario": {},
    }
    for field, key in (("class", "by_class"), ("scenario", "by_scenario")):
        for v in sorted({r[field] for r in done}):
            sub = [r for r in done if r[field] == v]
            out[key][v] = {"n": len(sub), "agreement": agree(sub)}
    return out
