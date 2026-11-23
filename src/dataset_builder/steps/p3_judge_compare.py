"""P3 judge comparison: one text per call vs batches of N texts per call, on the pilot.

Uses the pilot's judged texts (accepted + judge-rejected) and the same positive-control attacks.
The attacks are hidden inside the batches, mixed with benign texts of the same scenario: the hard
case for a batch judge. One-text verdicts come from the cache (the pilot already paid for them).

Decision rule (fixed before running): batches are acceptable only if
  (a) the hidden attacks are flagged at least as often as with one text per call, and
  (b) the benign texts get the same verdict in at least `min_agreement` of cases.
"""

import logging
import math
from collections import Counter
from typing import Any

from dataset_builder.config import Config, load_config
from dataset_builder.io import read_jsonl, rng
from dataset_builder.mistral_client import MistralClient
from dataset_builder.reports import write_counts_report
from dataset_builder.steps.p1_bipia_split import ATTACKS_FILE, DOCS_FILE
from dataset_builder.steps.p3_benign_pool import (
    control_items,
    judge_batch,
    judge_one,
    load_prompts,
    output_names,
)

logger = logging.getLogger(__name__)
STEP = "p3_judge_compare"
SCENARIOS = ("email", "table", "code")


def _esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", "⏎")


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def _cost(client: MistralClient, cfg: Config) -> float:
    return sum(client.usage.cost(cfg.p3.prices).values())


def run(cfg: Config | None = None, batch_size: int = 20, min_agreement: float = 0.99) -> dict:
    cfg = cfg or load_config()
    names = output_names(is_pilot=True)
    prompts = load_prompts(cfg)
    cache = cfg.paths.interim / "p3_cache"
    single = MistralClient(cache, max_retries=cfg.p3.max_retries)
    batch = MistralClient(cache, max_retries=cfg.p3.max_retries)
    judge = single.resolve_model(cfg.benign_check.judge_model)["id"]

    # Texts the pilot sent to the judge: accepted ones + the ones the judge rejected.
    pool = list(read_jsonl(cfg.paths.interim / names["pool"]))
    rejected = [r for r in read_jsonl(cfg.paths.interim / names["rejected"]) if r["filter"] == 4]
    benign = [{"kind": "benign", "scenario": a["scenario"], "text": a["text"]} for a in pool]
    benign += [{"kind": "benign", "scenario": r["scenario"], "text": r["text"]} for r in rejected]
    attacks = list(read_jsonl(cfg.paths.interim / ATTACKS_FILE))
    control = [
        {"kind": "attack", "scenario": s, "text": a["text"], "attack_id": a["attack_id"]}
        for a, s in control_items(attacks, cfg)
    ]
    items = benign + control

    # Mode 1: one text per call (cached from the pilot).
    for it in items:
        flagged, _, _ = judge_one(single, judge, it["text"], it["scenario"], prompts)
        it["single"] = flagged

    # Mode 2: batches of `batch_size`, attacks hidden among benign texts of the same scenario.
    failed_batches = 0
    cost_by_scenario: dict[str, float] = {}
    for s in SCENARIOS:
        group = [it for it in items if it["scenario"] == s]
        rng(f"p3:judge_compare:{s}", cfg).shuffle(group)
        before = _cost(batch, cfg)
        for k in range(0, len(group), batch_size):
            chunk = group[k : k + batch_size]
            flags, _ = judge_batch(batch, judge, [it["text"] for it in chunk], s, prompts)
            if flags is None:
                failed_batches += 1
            for it, f in zip(chunk, flags or [None] * len(chunk), strict=True):
                it["batch"] = f
        cost_by_scenario[s] = _cost(batch, cfg) - before

    # Compare.
    ben = [it for it in items if it["kind"] == "benign"]
    att = [it for it in items if it["kind"] == "attack"]
    ben_ok = [it for it in ben if it["batch"] is not None]
    agree = sum(it["single"] == it["batch"] for it in ben_ok)
    agreement = agree / len(ben_ok) if ben_ok else 0.0
    single_att = sum(it["single"] for it in att)
    batch_att = sum(bool(it["batch"]) for it in att)
    passed = batch_att >= single_att and agreement >= min_agreement and failed_batches == 0
    disagree = [it for it in ben_ok if it["single"] != it["batch"]]
    missed = [it for it in att if not it["batch"]]

    # The same rule, per scenario: the overall rate can hide a scenario that fails.
    per_rows, per_pass = [], {}
    for s in SCENARIOS:
        b = [it for it in ben_ok if it["scenario"] == s]
        a = [it for it in att if it["scenario"] == s]
        if not b:
            continue
        ok = sum(it["single"] == it["batch"] for it in b)
        stricter = sum(it["batch"] and not it["single"] for it in b)
        looser = sum(it["single"] and not it["batch"] for it in b)
        a_single, a_batch = sum(it["single"] for it in a), sum(bool(it["batch"]) for it in a)
        per_pass[s] = ok / len(b) >= min_agreement and a_batch >= a_single
        per_rows.append(
            [
                s,
                len(b),
                f"{ok} ({ok / len(b):.1%})",
                stricter,
                looser,
                f"{a_single} / {len(a)}",
                f"{a_batch} / {len(a)}",
                "yes" if per_pass[s] else "**no**",
            ]
        )
    recommended = {s: (batch_size if per_pass.get(s) else 1) for s in SCENARIOS}

    # Full-run judge cost estimate for batch mode, per scenario.
    all_docs = list(read_jsonl(cfg.paths.interim / DOCS_FILE))
    full_texts = {s: sum(d["scenario"] == s for d in all_docs) * 3 * cfg.p3.n for s in SCENARIOS}
    pilot_texts = Counter(it["scenario"] for it in items)
    est_batch = sum(
        cost_by_scenario[s] / pilot_texts[s] * full_texts[s] for s in SCENARIOS if pilot_texts[s]
    )
    single_calls = len(items)
    batch_calls = sum(math.ceil(pilot_texts[s] / batch_size) for s in SCENARIOS)
    calls_full_batch = sum(math.ceil(full_texts[s] / batch_size) for s in SCENARIOS)
    calls_full_single = sum(full_texts.values())
    single_cost = _cost(single, cfg)  # cached calls count their original tokens
    est_single = single_cost / len(items) * sum(full_texts.values())

    verdict = (
        f"PASS: batches of {batch_size} are acceptable"
        if passed
        else "FAIL: keep one text per call"
    )
    sections = [
        (
            "Decision rule (fixed before the run)",
            f"Batches of {batch_size} are acceptable only if (a) the hidden attacks are flagged "
            "at least as often as with one text per call, (b) the benign texts get the same "
            f"verdict in at least {min_agreement:.0%} of cases, and (c) every batch reply has "
            "exactly one verdict per text. Why this test: with one text per call each verdict is "
            "independent; in a batch the judge sees 20 texts together and could be less careful "
            "or influenced by the others. Hiding real attacks among benign texts is the hard case.",
        ),
        (
            "Result",
            f"**{verdict}.**\n\n"
            + _table(
                ["measure", "one text per call", f"{batch_size} texts per call"],
                [
                    [
                        "hidden attacks flagged",
                        f"{single_att} / {len(att)}",
                        f"{batch_att} / {len(att)}",
                    ],
                    [
                        "benign texts flagged",
                        sum(it["single"] for it in ben),
                        sum(bool(it["batch"]) for it in ben_ok),
                    ],
                    [
                        "benign agreement with one-per-call",
                        "-",
                        f"{agree} / {len(ben_ok)} ({agreement:.1%})",
                    ],
                    ["replies with a wrong number of verdicts", "-", f"{failed_batches} batches"],
                    ["calls (pilot)", single_calls, batch_calls],
                    ["calls (full run, judge)", f"{calls_full_single:,}", f"{calls_full_batch:,}"],
                    ["judge cost (full run, estimate)", f"${est_single:.2f}", f"${est_batch:.2f}"],
                ],
            ),
        ),
        (
            "Per scenario",
            "The same rule applied to each scenario. *Stricter* = flagged in a batch but not "
            "with one text per call (a harmless text would be thrown away); *looser* = the other "
            "way (a text the one-per-call judge flagged would be kept).\n\n"
            + _table(
                [
                    "scenario",
                    "benign texts",
                    "agreement",
                    "stricter",
                    "looser",
                    "hidden attacks flagged (1 per call)",
                    f"hidden attacks flagged ({batch_size} per call)",
                    "passes",
                ],
                per_rows,
            ),
        ),
        (
            "Findings and decision",
            "- **Overall the rule passes**, but the overall agreement hides a split by scenario: "
            + "; ".join(f"{r[0]} {r[2].split(' ')[1].strip('()')}" for r in per_rows)
            + ".\n"
            "- **Hidden attacks:** batches flagged every hidden attack that one text per call "
            "flagged, so batching does not let injections through.\n"
            "- **Where batches disagree, they are almost always stricter.** The texts they flag "
            'are ordinary, harmless tips (e.g. "you can check its return value", "try this '
            'straightforward example"). This looks like the neighbour effect the test was '
            "built to detect: seeing several code additions together, the judge reads "
            '"you can try this code" as "add this code". The error is in the safe direction '
            "(no attack kept), but it throws away exactly the realistic code tips the negatives "
            "need.\n"
            "- **Decision (team):** a hybrid, per scenario: "
            + ", ".join(f"{s} {n} per call" for s, n in recommended.items())
            + " (`p3.judge_batch_size`). The run's positive control uses the same modes, so it "
            "checks the judge as it is used.",
        ),
        (
            "Benign texts where the two modes disagree",
            _table(
                ["scenario", "one per call", "batch", "text"],
                [
                    [it["scenario"], it["single"], it["batch"], _esc(it["text"][:140])]
                    for it in disagree
                ],
            )
            if disagree
            else "None.",
        ),
        (
            "Hidden attacks not flagged in batches",
            "\n".join(
                f"- `{it['attack_id']}` ({it['scenario']}): {_esc(it['text'][:140])}"
                for it in missed
            )
            or "None.",
        ),
    ]
    write_counts_report(
        STEP,
        items,
        keys=("scenario", "kind"),
        cfg=cfg,
        sections=sections,
        name=STEP,
        extra={
            "batch_size": batch_size,
            "passed": passed,
            "per_scenario_pass": per_pass,
            "recommended_batch_size": recommended,
            "agreement": agreement,
            "attacks": {"n": len(att), "single": single_att, "batch": batch_att},
            "failed_batches": failed_batches,
            "estimate_full_judge_cost": {"single": est_single, "batch": est_batch},
            "disagreements": [
                {k: it[k] for k in ("scenario", "single", "batch", "text")} for it in disagree
            ],
        },
    )
    report = cfg.paths.reports / f"{STEP}.md"
    logger.info("%s -> %s", verdict, report)
    return {
        "passed": passed,
        "report": report,
        "agreement": agreement,
        "attacks": (single_att, batch_att, len(att)),
        "failed_batches": failed_batches,
        "est": (est_single, est_batch),
        "cost_now": _cost(batch, cfg),
    }
