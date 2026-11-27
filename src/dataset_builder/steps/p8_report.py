"""P8 report: reports/p8/p8_training_variants[_pilot].{md,json}.

Per split, scenario and class: rows, rule and judge rejections, pass rate, words changed, the
share base Shieldstral gets wrong (information only); rule rejections by reason; the balanced and
nested training subsets; time per phase (and, for the pilot, the projected full run); examples.
"""

import json
import re
from collections import Counter
from typing import Any

from dataset_builder.config import Config
from dataset_builder.reports import report_path

KEYS = ("split", "scenario", "label")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def summarise(rs: list[dict[str, Any]]) -> dict[str, Any]:
    c = Counter(r["status"] for r in rs)
    judged = c["kept"] + c["judge_rejected"] + c["judge_disagrees"]
    kept = [r for r in rs if r["status"] == "kept"]
    scored = [r for r in kept if r.get("p_yes_base") is not None]
    return {
        "rows": len(rs),
        "no_editable": c["no_editable"],
        "rule_rejected": c["rule_rejected"],
        "judge_disagrees": c["judge_disagrees"],
        "judge_rejected": c["judge_rejected"],
        "pending_judge": c["pending_judge"],
        "kept": len(kept),
        "pass_rate": len(kept) / len(rs) if rs else None,
        "judge_rejection_rate": (c["judge_rejected"] + c["judge_disagrees"]) / judged
        if judged
        else None,
        "retried": sum(r.get("attempts") == 2 for r in rs),
        "words_changed_pct": _mean(
            [r["words_changed"] / r["n_words"] for r in kept if r.get("n_words")]
        ),
        "base_wrong_share": _mean([float(r["wrong"]) for r in scored]),
        "seconds_per_row": _mean([r.get("seconds", 0.0) for r in rs]),
    }


def _reason(text: str) -> str:
    """A rejection reason without its numbers and tokens, to count kinds."""
    text = re.sub(r"'[^']*'", "'...'", text)
    return re.sub(r"\d+", "N", text)


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def write(
    cfg: Config,
    results: list[dict[str, Any]],
    notes: dict[str, Any],
    info: dict[str, Any],
    times: dict[str, float],
    seconds: float,
    *,
    pilot: bool = False,
    assembly: dict[str, Any] | None = None,
    built: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    assert cfg.p8 is not None
    p8 = cfg.p8
    name = "p8_training_variants_pilot" if pilot else "p8_training_variants"
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        buckets.setdefault(f"{r['split']}/{r['scenario']}/{r['label']}", []).append(r)
    by_bucket = {k: summarise(v) for k, v in sorted(buckets.items())}
    overall = summarise(results)
    reasons = Counter(_reason(r["reason"]) for r in results if r["status"] == "rule_rejected")
    first = Counter(_reason(r["first_reason"]) for r in results if r.get("first_reason"))

    lines = [
        f"# P8 training variants{' (pilot)' if pilot else ''}",
        "",
        f"- Generator: `{info.get('generator')}` (revision {info.get('generator_revision')}, "
        f"8-bit {info.get('generator_8bit')}), decoding {info.get('decoding')}; "
        f"{info.get('generator_tokens_per_s')} new tokens/s; peak GPU "
        f"{info.get('generator_peak_gpu_gb')} GB.",
        f"- Judge: `{info.get('judge')}` (revision {info.get('judge_revision')}), "
        f"{info.get('judge_calls', 0)} calls, {info.get('judge_unparsed', 0)} unusable answers.",
        f"- Base Shieldstral (information only): `{info.get('victim')}`.",
        f"- Rules: at most {cfg.textfooler.max_span_fraction:.0%} of the span's words changed; "
        f"word count within {p8.word_count_tolerance:.0%}; protected tokens kept; code block "
        "untouched; text outside the span unchanged. One retry on a rule failure.",
        "- Selection: every variant the judge accepts (team decision); classes balanced per split.",
        "",
        "## Overall",
        "",
        f"- Rows: {overall['rows']:,}; kept {overall['kept']:,} "
        f"(pass rate {_pct(overall['pass_rate'])}); rule rejected {overall['rule_rejected']:,}; "
        f"judge rejected {overall['judge_rejected']:,}; judge disagrees with the original's label "
        f"{overall['judge_disagrees']:,}; retried {overall['retried']:,}.",
        f"- Base Shieldstral gets {_pct(overall['base_wrong_share'])} of the kept variants wrong.",
    ]
    if not pilot:
        lines.append(
            f"- Expected about {p8.expected_usable:,} usable of about "
            f"{2 * (p8.train_pairs + p8.validation_pairs):,}; stop rule: pass rate below "
            f"{p8.min_pass_rate:.0%}."
        )
    lines += ["", "## Sampling", ""]
    for s, n in notes.items():
        short = f"; short: {n['short']}" if n["short"] else ""
        lines.append(
            f"- {s}: pairs wanted {n['wanted']}, available {n['available']}, "
            f"taken {n['taken']}{short}"
        )
    lines += [
        "",
        "## Outcomes per split, scenario and class",
        "",
        _table(
            [
                "split/scenario/label",
                "rows",
                "rule rej.",
                "judge disagrees",
                "judge rej.",
                "kept",
                "pass rate",
                "judge rejection rate",
                "words changed",
                "base wrong",
            ],
            [
                [
                    k,
                    v["rows"],
                    v["rule_rejected"],
                    v["judge_disagrees"],
                    v["judge_rejected"],
                    v["kept"],
                    _pct(v["pass_rate"]),
                    _pct(v["judge_rejection_rate"]),
                    _pct(v["words_changed_pct"]),
                    _pct(v["base_wrong_share"]),
                ]
                for k, v in by_bucket.items()
            ],
        ),
        "",
        "## Rule rejections by reason (final, after the retry)",
        "",
        _table(["reason", "rows"], [[k, v] for k, v in reasons.most_common(12)])
        if reasons
        else "None.",
        "",
        "First-attempt failures (retried): "
        + (", ".join(f"{k} ({v})" for k, v in first.most_common(6)) or "none")
        + ".",
        "",
        "## Time",
        "",
        "- Phases (s): "
        + ", ".join(f"{k} {v:.0f}" for k, v in times.items())
        + f"; total {seconds:.0f}.",
    ]
    rows_done = max(len(results), 1)
    full_rows = 2 * (p8.train_pairs + p8.validation_pairs)
    projected = sum(times.values()) / rows_done * full_rows / 3600
    if pilot:
        lines.append(
            f"- Projected full run ({full_rows:,} rows): about {projected:.1f} h "
            "(model loading counted once per phase in the pilot)."
        )
    if assembly:
        lines += ["", "## Assembled", ""]
        for s, a in assembly.items():
            lines.append(
                f"- {s}: {a['per_class']:,} per class; "
                f"dropped for balance {a['dropped_for_balance']}; "
                f"nested subsets {a['nested']}"
                + (f"; **short for {a['short']}**" if a["short"] else "")
            )
        if built:
            counts = Counter(
                (s, r.scenario, r.label, r.position) for s, rs in built.items() for r in rs
            )
            lines += [
                "",
                _table(
                    ["split", "scenario", "label", "position", "rows"],
                    [[*k, v] for k, v in sorted(counts.items())],
                ),
            ]
    kept = [r for r in results if r["status"] == "kept"][:10]
    rejected = [r for r in results if r["status"] in ("rule_rejected", "judge_rejected")][:10]
    lines += ["", "## Examples", ""]
    for title, rs in (("Kept", kept), ("Rejected", rejected)):
        lines.append(f"### {title}")
        for r in rs:
            why = r.get("reason") or (r.get("verdict") or {}).get("reason", "")
            lines.append(
                f"- **{r['split']}/{r['scenario']}/{r['label']}** {why}  \n"
                f"  original: {r['span'].strip()[:240]!r}  \n"
                f"  variant: {r.get('new_span', '').strip()[:240]!r}"
            )
        lines.append("")
    md = report_path(cfg, f"{name}.md")
    md.write_text("\n".join(lines), encoding="utf-8")
    split_counts = {s: len(rs) for s, rs in (built or {}).items()}
    data = {
        "pilot": pilot,
        "overall": overall,
        "by_bucket": by_bucket,
        "rule_reasons": dict(reasons),
        "sampling": notes,
        "assembly": assembly,
        "split": split_counts,
        "times": times,
        "seconds": seconds,
        "projected_hours": projected if pilot else None,
        "info": info,
    }
    js = report_path(cfg, f"{name}.json")
    js.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return {"md": md, "json": js, "summary": overall}
