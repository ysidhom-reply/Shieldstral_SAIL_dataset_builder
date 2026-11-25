"""P6: TextFooler test variants against base Shieldstral, and benign twins (dataset version v1).

What this step does, per run:
1. pairs (a positive and its benign twin) are sampled before any attack: stratified by scenario,
   attack group and position, capped per document, seeded; the ids are stored;
2. malicious test positives are attacked with TextAttack's TextFooler on the inserted span only.
   The victim (base Shieldstral) scores the whole document. Originals it already misses are
   skipped and counted. A successful variant is kept only if the rules and the judge accept it;
3. for the kept positives, their benign twins give two sets: plain copies (`control`) and
   TextFooler-perturbed twins (not filtered on the victim's output; the judge must still find
   them harmless);
4. a counts report, the data/out file and a CSV for the human check are written.

Work goes in chunks (judge the originals, attack, judge the successes) so the victim and the judge
alternate per chunk, never per example. Every candidate's outcome is appended to
data/interim/p6_results[_pilot].jsonl, so a stopped run resumes.

The attack, the pilot and the full run need a GPU: this module only wires them. Models are given
to `run` (tests pass fakes); by default they are built from the config.
"""

import hashlib
import json
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dataset_builder.config import Config, TextFoolerCfg, load_config
from dataset_builder.io import prepare_out, read_jsonl, read_parquet, write_jsonl, write_parquet
from dataset_builder.reports import CountMismatchError
from dataset_builder.schema import TextFoolerSample
from dataset_builder.steps import p6_report
from dataset_builder.steps.p6_attack import Engine, TextFoolerEngine
from dataset_builder.steps.p6_judge import Judge, LocalJudge
from dataset_builder.steps.p6_sampling import (
    SCENARIOS,
    attempt_orders,
    make_pairs,
    pilot_pairs,
    sample_records,
    stratified_order,
)
from dataset_builder.steps.p6_span import (
    WORD,
    check_variant,
    locate_span,
    modifiable_indices,
    nltk_entities,
    protected_tokens,
)
from dataset_builder.steps.p6_victim import CachedScorer, ShieldstralScorer

logger = logging.getLogger(__name__)

# Outcomes of one candidate, in the order they are decided.
STATUSES = (
    "no_editable",  # no word of the span may be changed (all protected)
    "judge_disagrees",  # the judge disagrees with the label on the original (or gave no answer)
    "skipped",  # malicious: the victim already misses the original, nothing to flip
    "failed",  # the attack did not reach the goal (malicious only)
    "rule_rejected",  # a rule check failed (malicious: after a success)
    "judge_rejected",  # the judge says the meaning or the label changed
    "kept",
)


def variant_id(prefix: str, source_id: str, salt: str = "") -> str:
    return prefix + hashlib.sha256(f"{source_id}|{salt}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- one chunk of candidates


def process_chunk(
    items: list[dict[str, Any]],
    engine: Engine,
    judge: Judge,
    tf: TextFoolerCfg,
    benign: dict[str, str],
    entities: Callable[[str], list[str]] | None,
) -> list[dict[str, Any]]:
    """Candidates of one chunk, end to end.

    Each item: {"kind": "malicious" | "twin", "row": <bipia row>, "pair_id", "doc"}. Returns one
    record per item (the lines of p6_results.jsonl). Three phases: the judge reads the originals,
    the victim attacks, the judge reads the successes."""
    recs: list[dict[str, Any]] = []
    live: list[dict[str, Any]] = []
    for it in items:
        row = it["row"]
        label = int(row["label"])
        span = locate_span(row, benign.get(row.get("benign_id") or ""))
        tokens = protected_tokens(span.text, entities)
        rec = {
            "key": f"{it['kind']}:{row['sample_id']}",
            "kind": it["kind"],
            "sample_id": row["sample_id"],
            "pair_id": it["pair_id"],
            "doc": it["doc"],
            "scenario": row["scenario"],
            "label": label,
            "attack_group": row.get("attack_group"),
            "position": row.get("position"),
            "span": span.text,
            "inserted": span.inserted(),
            "protected_tokens": tokens,
            "n_words": len(WORD.findall(span.text)),
            "seconds": 0.0,
        }
        recs.append(rec)
        if not modifiable_indices(WORD.findall(span.text), tokens):
            rec["status"] = "no_editable"
        else:
            live.append({"rec": rec, "row": row, "span": span, "tokens": tokens})

    # phase 1: the judge must agree with the label on the original
    t0 = time.monotonic()
    vs = judge.verdicts(
        [(x["span"].inserted(),) * 2 + (x["rec"]["label"], x["rec"]["scenario"]) for x in live]
    )
    judge_s = time.monotonic() - t0
    attack: list[dict[str, Any]] = []
    for x, v in zip(live, vs, strict=True):
        if v is None or not v.label_still_correct:
            x["rec"]["status"] = "judge_disagrees"
            x["rec"]["original_verdict"] = v.as_dict() if v else None
        else:
            attack.append(x)

    # phase 2: score the originals, then attack (victim only)
    thr = tf.threshold
    p0 = engine.score(
        [(x["row"]["instruct"], x["row"]["query"], x["row"]["document"]) for x in attack]
    )
    judge_me: list[dict[str, Any]] = []
    for x, p_orig in zip(attack, p0, strict=True):
        rec, row, span = x["rec"], x["row"], x["span"]
        rec["p_orig"] = p_orig
        if rec["kind"] == "malicious" and p_orig < thr:
            rec["status"] = "skipped"  # already missed: no prediction to change
            continue
        # a twin the victim already calls positive has nothing to flip: perturb without feedback
        feedback = rec["kind"] == "malicious" or p_orig < thr
        t1 = time.monotonic()
        out = engine.attack(
            span, rec["label"], x["tokens"], row["instruct"], row["query"], feedback
        )
        rec["seconds"] += time.monotonic() - t1
        rec |= {
            "queries": out["queries"],
            "p_new": out["p_new"],
            "new_span": out["text"],
            "feedback": feedback,
        }
        if rec["kind"] == "malicious" and not out["p_new"] < thr:
            rec["status"] = "failed"
            continue
        if rec["kind"] == "twin":
            rec["flipped"] = out["p_new"] >= thr
        reason, changed, _ = check_variant(span, out["text"], x["tokens"], tf.max_span_fraction)
        rec["words_changed"] = changed
        if reason:
            rec["status"], rec["reason"] = "rule_rejected", reason
            continue
        judge_me.append(x)

    # phase 3: the judge reads what succeeded
    t0 = time.monotonic()
    vs = judge.verdicts(
        [
            (
                x["span"].inserted(),
                x["span"].inserted(x["rec"]["new_span"]),
                x["rec"]["label"],
                x["rec"]["scenario"],
            )
            for x in judge_me
        ]
    )
    judge_s += time.monotonic() - t0
    for x, v in zip(judge_me, vs, strict=True):
        x["rec"]["verdict"] = v.as_dict() if v else None
        x["rec"]["status"] = "kept" if v is not None and v.accepted else "judge_rejected"
    for rec in recs:  # the judge's time is shared evenly (it ran in a batch)
        rec["seconds"] = round(rec["seconds"] + judge_s / max(len(recs), 1), 3)
    return recs


# ---------------------------------------------------------------- loops


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    return {r["key"]: r for r in read_jsonl(path)} if path.is_file() else {}


def append_results(path: Path, recs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def effective_targets(
    targets: dict[str, int], kept: dict[str, int], exhausted: set[str], max_per: int
) -> dict[str, int]:
    """A scenario that cannot reach its target (candidates or cap exhausted) hands its shortfall to
    the scenarios still running, up to `max_per` each. Why: the total stays near the brief's 1,000
    while the floor per scenario is kept."""
    out = dict(targets)
    short = sum(max(targets[s] - kept.get(s, 0), 0) for s in exhausted if s in targets)
    running = [s for s in targets if s not in exhausted]
    for s in running:
        out[s] = min(max_per, targets[s] + -(-short // len(running))) if running else targets[s]
    return out


class Runner:
    """Holds what the loops share: models, config, the results file."""

    def __init__(
        self,
        cfg: Config,
        engine: Engine,
        judge: Judge,
        benign: dict[str, str],
        entities: Callable[[str], list[str]] | None,
        results: dict[str, dict[str, Any]],
        path: Path,
    ) -> None:
        self.cfg, self.tf = cfg, cfg.textfooler
        self.engine, self.judge, self.benign, self.entities = engine, judge, benign, entities
        self.results, self.path = results, path

    def work(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        todo = [it for it in items if f"{it['kind']}:{it['row']['sample_id']}" not in self.results]
        recs: list[dict[str, Any]] = []
        for i in range(0, len(todo), self.tf.chunk_size):
            chunk = process_chunk(
                todo[i : i + self.tf.chunk_size],
                self.engine,
                self.judge,
                self.tf,
                self.benign,
                self.entities,
            )
            for r in chunk:
                self.results[r["key"]] = r
            append_results(self.path, chunk)
            recs += chunk
        return recs

    def kept(self, kind: str, scenario: str, pair_ids: set[str] | None = None) -> int:
        return sum(
            1
            for r in self.results.values()
            if r["kind"] == kind
            and r["scenario"] == scenario
            and r["status"] == "kept"
            and (pair_ids is None or r["pair_id"] in pair_ids)
        )

    def malicious(
        self, orders: dict[str, list[dict[str, Any]]], targets: dict[str, int], redistribute: bool
    ) -> None:
        """Attack positives in each scenario's order until its target of kept variants."""
        cursor = dict.fromkeys(SCENARIOS, 0)
        share = max(self.tf.chunk_size // len(SCENARIOS), 1)
        last = time.monotonic()
        while True:
            kept = {
                s: self.kept("malicious", s, {p["pair_id"] for p in orders[s]}) for s in SCENARIOS
            }
            exhausted = {s for s in SCENARIOS if cursor[s] >= len(orders[s])}
            eff = (
                effective_targets(targets, kept, exhausted, self.tf.max_per_scenario)
                if redistribute
                else targets
            )
            active = [s for s in SCENARIOS if kept[s] < eff.get(s, 0) and s not in exhausted]
            if not active:
                return
            items = []
            for s in active:
                take = orders[s][cursor[s] : cursor[s] + share]
                cursor[s] += len(take)
                items += [
                    {"kind": "malicious", "row": p["pos"], "pair_id": p["pair_id"], "doc": p["doc"]}
                    for p in take
                ]
            self.work(items)
            if time.monotonic() - last > 60:
                last = time.monotonic()
                logger.info("P6 kept: %s | processed %d", kept, len(self.results))

    def twins(
        self, kept_pairs: dict[str, list[dict[str, Any]]], rest: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Perturbed twins: as many kept twins per scenario as kept positives. The twins of the
        kept positives come first; a twin the judge rejects is replaced by the next pair's."""
        for s in SCENARIOS:
            need = len(kept_pairs[s])
            queue = [*kept_pairs[s], *rest[s]]
            i = 0
            ids = {p["pair_id"] for p in queue}
            while self.kept("twin", s, ids) < need and i < len(queue):
                take = need - self.kept("twin", s, ids)
                batch = queue[i : i + max(take, 1)]
                i += len(batch)
                self.work(
                    [
                        {"kind": "twin", "row": p["neg"], "pair_id": p["pair_id"], "doc": p["doc"]}
                        for p in batch
                    ]
                )


# ---------------------------------------------------------------- assembly


def _variant_row(
    src: dict[str, Any],
    rec: dict[str, Any],
    benign: dict[str, str],
    scorer_name: str,
    judge_name: str,
    method: str,
    prefix: str,
) -> TextFoolerSample:
    span = locate_span(src, benign.get(src.get("benign_id") or ""))
    new = rec["new_span"]
    fields = {k: src[k] for k in TextFoolerSample.model_fields if k in src}
    fields |= {
        "sample_id": variant_id(prefix, src["sample_id"]),
        "parent_id": src["sample_id"],
        "source": "bipia_textfooler",
        "document": span.document(new),
        "label_provenance": "validated",
        "generation_method": method,
        "protected_tokens": rec["protected_tokens"],
        "victim_model": scorer_name,
        "p_yes_base": rec["p_new"],
        "judge_model": judge_name,
    }
    if rec["label"] == 1:
        fields["injection_span"] = span.inserted(new)
    else:
        start = src["benign_span"][0]
        fields["benign_span"] = [start, start + len(span.inserted(new))]
    return TextFoolerSample(**fields)


def assemble(
    runner: Runner,
    sources: dict[str, dict[str, Any]],
    pairs_by_id: dict[str, dict[str, Any]],
    victim: str,
    twin_cap: int,
) -> tuple[list[TextFoolerSample], dict[str, Any]]:
    """The rows of data/out: kept malicious variants, plain twins and perturbed twins."""
    judge_name = runner.judge.name
    kept_mal = [
        r for r in runner.results.values() if r["kind"] == "malicious" and r["status"] == "kept"
    ]
    rows: list[TextFoolerSample] = []
    for r in kept_mal:
        rows.append(
            _variant_row(
                sources[r["sample_id"]], r, runner.benign, victim, judge_name, "textfooler", "tf-"
            )
        )
    main = [r for r in kept_mal if r["attack_group"] != runner.tf.translation_group]
    counts: dict[str, Any] = {"malicious_kept": Counter(r["scenario"] for r in kept_mal)}
    # controls: the benign twins of the kept (main) positives, copied unchanged
    controls = []
    for r in main:
        neg = pairs_by_id[r["pair_id"]]["neg"]
        controls.append(neg)
    ps = (
        runner.engine.score([(n["instruct"], n["query"], n["document"]) for n in controls])
        if controls
        else []
    )
    control_p: list[tuple[str, float]] = []  # (document, p(yes)) for the false-positive rate
    for r, neg, p in zip(main, controls, ps, strict=True):
        control_p.append((r["doc"], p))
        fields = {k: neg[k] for k in TextFoolerSample.model_fields if k in neg}
        fields |= {
            "sample_id": variant_id("ctl-", neg["sample_id"]),
            "parent_id": neg["sample_id"],
            "source": "bipia_textfooler",
            "generation_method": "control",
            "victim_model": victim,
            "p_yes_base": p,
        }
        rows.append(TextFoolerSample(**fields))
    kept_pairs = {r["pair_id"] for r in main}
    twins = [r for r in runner.results.values() if r["kind"] == "twin" and r["status"] == "kept"]
    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in twins:  # the kept positives' twins first, then replacements, in attack order
        by_scenario[r["scenario"]].append(r)
    need = Counter(r["scenario"] for r in main)
    chosen = []
    for s in SCENARIOS:
        pool = sorted(by_scenario[s], key=lambda r: r["pair_id"] not in kept_pairs)
        chosen += pool[: need[s]]
    for r in chosen:
        rows.append(
            _variant_row(
                sources[r["sample_id"]], r, runner.benign, victim, judge_name, "textfooler", "tfb-"
            )
        )
    counts |= {"control": len(controls), "perturbed_twins": len(chosen), "twin_cap": twin_cap}
    counts["control_p"] = control_p
    return rows, counts


# ---------------------------------------------------------------- entry point


def build_models(cfg: Config) -> tuple[Engine, Judge]:
    scorer = CachedScorer(ShieldstralScorer(cfg.textfooler))
    return TextFoolerEngine(cfg.textfooler, scorer), LocalJudge(cfg.textfooler)


def run(
    cfg: Config | None = None,
    *,
    force: bool = False,
    is_pilot: bool = True,
    engine: Engine | None = None,
    judge: Judge | None = None,
    entities: Callable[[str], list[str]] | bool | None = True,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    tf = cfg.textfooler
    t0 = time.monotonic()
    rows = read_parquet(cfg.paths.out / "bipia" / "test.parquet")
    sources = {r["sample_id"]: r for r in rows}
    pairs = make_pairs(rows)
    dropped = len(rows) - 2 * len(pairs)
    if dropped:
        raise CountMismatchError(f"P6: {dropped} test rows have no partner with the same parent_id")
    pairs_by_id = {p["pair_id"]: p for p in pairs}
    needed = {r["benign_id"] for r in rows if r.get("benign_id")}
    pool = cfg.paths.interim / "benign_pool.jsonl"
    benign = (
        {b["benign_id"]: b["text"] for b in read_jsonl(pool) if b["benign_id"] in needed}
        if pool.is_file()
        else {}
    )
    ents = nltk_entities(cfg) if entities is True else (entities or None)
    if engine is None or judge is None:
        built_engine, built_judge = build_models(cfg)
        engine, judge = engine or built_engine, judge or built_judge
    path = cfg.paths.interim / ("p6_results_pilot.jsonl" if is_pilot else "p6_results.jsonl")
    results = load_results(path)
    if results:
        logger.info("P6: resuming, %d candidates already processed", len(results))
    runner = Runner(cfg, engine, judge, benign, ents, results, path)

    if is_pilot:
        sample = pilot_pairs(pairs, cfg)
        orders = {s: [p for p in sample if p["scenario"] == s] for s in SCENARIOS}
        runner.work(
            [
                {"kind": "malicious", "row": p["pos"], "pair_id": p["pair_id"], "doc": p["doc"]}
                for p in sample
            ]
        )
        runner.work(
            [
                {"kind": "twin", "row": p["neg"], "pair_id": p["pair_id"], "doc": p["doc"]}
                for p in sample
            ]
        )
        wanted = {p["pair_id"] for p in sample}
        res_list = [r for r in results.values() if r["pair_id"] in wanted]
        write_jsonl(cfg.paths.interim / "p6_sample_ids_pilot.jsonl", sample_records(orders))
        out = p6_report.write_pilot(cfg, res_list, engine, judge, time.monotonic() - t0)
        return out | {"results": path}

    orders = attempt_orders(pairs, cfg)
    write_jsonl(cfg.paths.interim / "p6_sample_ids.jsonl", sample_records(orders))
    runner.malicious(orders, tf.targets, redistribute=True)
    if tf.translation_slice:
        t_orders = {s: stratified_order(pairs, cfg, s, translation=True) for s in SCENARIOS}
        per = -(-tf.translation_slice // len(SCENARIOS))
        runner.malicious(t_orders, dict.fromkeys(SCENARIOS, per), redistribute=False)
    kept_ids = {
        r["pair_id"] for r in results.values() if r["kind"] == "malicious" and r["status"] == "kept"
    }
    main_ids = {p["pair_id"] for s in SCENARIOS for p in orders[s]}
    kept_pairs = {s: [p for p in orders[s] if p["pair_id"] in kept_ids] for s in SCENARIOS}
    rest = {
        s: [p for p in orders[s] if p["pair_id"] not in kept_ids and p["pair_id"] in main_ids]
        for s in SCENARIOS
    }
    runner.twins(kept_pairs, rest)

    info = engine.info() | judge.info()
    victim = info.get("victim", tf.victim_model)
    samples, counts = assemble(runner, sources, pairs_by_id, victim, tf.twin_sets)
    ids = [s.sample_id for s in samples]
    if len(set(ids)) != len(ids):
        raise CountMismatchError("P6: duplicate sample_id")
    if not samples:
        raise CountMismatchError("P6: nothing was kept")
    out_path = prepare_out(
        cfg.paths.out / "bipia_textfooler" / "test.parquet", force=force, cfg=cfg
    )
    write_parquet(out_path, samples)
    report = p6_report.write_full(
        cfg, samples, list(results.values()), counts, info, orders, time.monotonic() - t0
    )
    human = p6_report.write_human_check(cfg, list(results.values()), sources)
    return {
        "out": out_path,
        "results": path,
        "report": report,
        "human_check": human,
        "counts": counts,
    }
