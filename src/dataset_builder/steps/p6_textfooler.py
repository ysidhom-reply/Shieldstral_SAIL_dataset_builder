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

Work goes in rounds of a few hundred candidates, with one model on the GPU at a time:
- victim phase (Shieldstral loaded): score the originals, attack, apply the rules; every outcome is
  saved, the successes as `pending_judge`. Then the victim is unloaded;
- judge phase (gpt-oss loaded): for each success, the judge must agree with the label on the
  original, then accept the variant. Then the judge is unloaded.
The size of the next round comes from the keep rate seen so far. Every outcome is appended to
data/interim/p6_results[_pilot].jsonl, so a stopped run resumes (a `pending_judge` line is judged
first).

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
from dataset_builder.steps.p6_judge import Judge, build_judge
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
    "skipped",  # malicious: the victim already misses the original, nothing to flip
    "failed",  # the attack did not reach the goal (malicious only)
    "rule_rejected",  # a rule check failed (malicious: after a success)
    "pending_judge",  # waiting for the judge phase (only in the results file between phases)
    "judge_disagrees",  # the judge disagrees with the label on the original (or gave no answer)
    "judge_rejected",  # the judge says the meaning or the label changed
    "kept",
)


def variant_id(prefix: str, source_id: str, salt: str = "") -> str:
    return prefix + hashlib.sha256(f"{source_id}|{salt}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------- the two phases of a round


def victim_phase(
    items: list[dict[str, Any]],
    engine: Engine,
    tf: TextFoolerCfg,
    benign: dict[str, str],
    entities: Callable[[str], list[str]] | None,
) -> list[dict[str, Any]]:
    """Victim work for a batch of candidates: score the originals, attack, apply the rules.

    Each item: {"kind": "malicious" | "twin", "row": <bipia row>, "pair_id", "doc"}. Returns one
    record per item (the lines of p6_results.jsonl). A success ends as `pending_judge`."""
    recs: list[dict[str, Any]] = []
    spans: list[tuple[Any, list[str]]] = []
    for it in items:
        row = it["row"]
        span = locate_span(row, benign.get(row.get("benign_id") or ""))
        tokens = protected_tokens(span.text, entities)
        spans.append((span, tokens))
        recs.append(
            {
                "key": f"{it['kind']}:{row['sample_id']}",
                "kind": it["kind"],
                "sample_id": row["sample_id"],
                "pair_id": it["pair_id"],
                "doc": it["doc"],
                "scenario": row["scenario"],
                "label": int(row["label"]),
                "attack_group": row.get("attack_group"),
                "position": row.get("position"),
                "span": span.text,
                "inserted": span.inserted(),
                "protected_tokens": tokens,
                "n_words": len(WORD.findall(span.text)),
                "seconds": 0.0,
            }
        )
    # every original is scored: a twin's score is also its plain control's score
    p0 = engine.score(
        [(it["row"]["instruct"], it["row"]["query"], it["row"]["document"]) for it in items]
    )
    thr = tf.threshold
    for it, rec, (span, tokens), p_orig in zip(items, recs, spans, p0, strict=True):
        rec["p_orig"] = p_orig
        if not modifiable_indices(WORD.findall(span.text), tokens):
            rec["status"] = "no_editable"
            continue
        if rec["kind"] == "malicious" and p_orig < thr:
            rec["status"] = "skipped"  # already missed: no prediction to change
            continue
        # a twin the victim already calls positive has nothing to flip: perturb without feedback
        feedback = rec["kind"] == "malicious" or p_orig < thr
        row = it["row"]
        t1 = time.monotonic()
        out = engine.attack(span, rec["label"], tokens, row["instruct"], row["query"], feedback)
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
        reason, changed, _ = check_variant(span, out["text"], tokens, tf.max_span_fraction)
        rec["words_changed"] = changed
        if reason:
            rec["status"], rec["reason"] = "rule_rejected", reason
            continue
        rec["status"] = "pending_judge"
    for rec in recs:
        rec["seconds"] = round(rec["seconds"], 3)
    return recs


def modified_text(rec: dict[str, Any]) -> str:
    """The inserted text with the attacked span (works from the saved record alone)."""
    return rec["new_span"] + rec["inserted"][len(rec["span"]) :]


def judge_phase(recs: list[dict[str, Any]], judge: Judge) -> list[dict[str, Any]]:
    """Judge the `pending_judge` records in one call (the judge loads once). For each: the label
    on the original first (passed as both texts), then the variant. Updates the records."""
    if not recs:
        return recs
    queries = []
    for r in recs:
        queries.append((r["inserted"], r["inserted"], r["label"], r["scenario"]))
        queries.append((r["inserted"], modified_text(r), r["label"], r["scenario"]))
    t0 = time.monotonic()
    vs = judge.verdicts(queries)
    share = (time.monotonic() - t0) / len(recs)  # the judge ran in a batch: time shared evenly
    for i, r in enumerate(recs):
        v_orig, v = vs[2 * i], vs[2 * i + 1]
        r["original_verdict"] = v_orig.as_dict() if v_orig else None
        r["verdict"] = v.as_dict() if v else None
        if v_orig is None or not v_orig.label_still_correct:
            r["status"] = "judge_disagrees"
        else:
            r["status"] = "kept" if v is not None and v.accepted else "judge_rejected"
        r["seconds"] = round(r["seconds"] + share, 3)
    return recs


def release(model: Any) -> None:
    """Free a model's GPU memory if it can (fakes and servers cannot)."""
    fn = getattr(model, "release", None)
    if callable(fn):
        fn()


def process_round(
    items: list[dict[str, Any]],
    engine: Engine,
    judge: Judge,
    tf: TextFoolerCfg,
    benign: dict[str, str],
    entities: Callable[[str], list[str]] | None,
) -> list[dict[str, Any]]:
    """Both phases on a batch, without saving (the runner saves between phases)."""
    recs = victim_phase(items, engine, tf, benign, entities)
    release(engine)
    judge_phase([r for r in recs if r["status"] == "pending_judge"], judge)
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


def round_take(remaining: int, done: int, kept: int, share: int) -> int:
    """Candidates to attempt next for `remaining` kept ones: from the keep rate seen so far, at
    most `share`. Before anything was kept the whole share is tried."""
    if remaining <= 0:
        return 0
    if done == 0 or kept == 0:
        return share
    return max(min(-(-remaining * done // kept), share), 1)


class Runner:
    """Holds what the rounds share: models, config, the results file."""

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
        self.rounds = 0

    def _save(self, recs: list[dict[str, Any]]) -> None:
        for r in recs:
            self.results[r["key"]] = r
        append_results(self.path, recs)

    def work(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One round: the victim phase on the new items (saved every `chunk_size`), the victim
        unloaded, then the judge phase on everything pending (this round and any resumed one)."""
        todo = [it for it in items if f"{it['kind']}:{it['row']['sample_id']}" not in self.results]
        recs: list[dict[str, Any]] = []
        for i in range(0, len(todo), self.tf.chunk_size):
            chunk = victim_phase(
                todo[i : i + self.tf.chunk_size], self.engine, self.tf, self.benign, self.entities
            )
            self._save(chunk)
            recs += chunk
        release(self.engine)
        pending = [r for r in self.results.values() if r["status"] == "pending_judge"]
        if pending:
            self._save(judge_phase(pending, self.judge))
        self.rounds += 1
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

    def done(self, kind: str, scenario: str, pair_ids: set[str]) -> int:
        return sum(
            1
            for r in self.results.values()
            if r["kind"] == kind and r["scenario"] == scenario and r["pair_id"] in pair_ids
        )

    def malicious(
        self, orders: dict[str, list[dict[str, Any]]], targets: dict[str, int], redistribute: bool
    ) -> None:
        """Rounds of positives, all scenarios together, until each reaches its target of kept
        variants or runs out of candidates."""
        cursor = dict.fromkeys(SCENARIOS, 0)
        ids = {s: {p["pair_id"] for p in orders[s]} for s in SCENARIOS}
        while True:
            kept = {s: self.kept("malicious", s, ids[s]) for s in SCENARIOS}
            exhausted = {s for s in SCENARIOS if cursor[s] >= len(orders[s])}
            eff = (
                effective_targets(targets, kept, exhausted, self.tf.max_per_scenario)
                if redistribute
                else targets
            )
            active = [s for s in SCENARIOS if kept[s] < eff.get(s, 0) and s not in exhausted]
            if not active:
                return
            share = max(self.tf.round_size // len(active), 1)
            items = []
            for s in active:
                n = round_take(eff[s] - kept[s], self.done("malicious", s, ids[s]), kept[s], share)
                take = orders[s][cursor[s] : cursor[s] + n]
                cursor[s] += len(take)
                items += [
                    {"kind": "malicious", "row": p["pos"], "pair_id": p["pair_id"], "doc": p["doc"]}
                    for p in take
                ]
            self.work(items)
            logger.info("P6 round %d, kept: %s", self.rounds, kept)

    def twins(
        self, kept_pairs: dict[str, list[dict[str, Any]]], rest: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Perturbed twins: as many kept twins per scenario as kept positives. The first round
        takes the kept positives' twins; a twin the judge rejects is replaced in a later round by
        the next pair's."""
        queue = {s: [*kept_pairs[s], *rest[s]] for s in SCENARIOS}
        ids = {s: {p["pair_id"] for p in queue[s]} for s in SCENARIOS}
        need = {s: len(kept_pairs[s]) for s in SCENARIOS}
        cursor = dict.fromkeys(SCENARIOS, 0)
        while True:
            kept = {s: self.kept("twin", s, ids[s]) for s in SCENARIOS}
            active = [s for s in SCENARIOS if kept[s] < need[s] and cursor[s] < len(queue[s])]
            if not active:
                return
            items = []
            for s in active:
                done = self.done("twin", s, ids[s])
                n = need[s] if done == 0 else round_take(need[s] - kept[s], done, kept[s], need[s])
                take = queue[s][cursor[s] : cursor[s] + n]
                cursor[s] += len(take)
                items += [
                    {"kind": "twin", "row": p["neg"], "pair_id": p["pair_id"], "doc": p["doc"]}
                    for p in take
                ]
            self.work(items)


# ---------------------------------------------------------------- assembly


def _variant_row(
    src: dict[str, Any],
    rec: dict[str, Any],
    benign: dict[str, str],
    scorer_name: str,
    judge_name: str | None,
    method: str,
    prefix: str,
    extra: dict[str, Any] | None = None,
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
    fields |= extra or {}
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
    kept_mal = [
        r for r in runner.results.values() if r["kind"] == "malicious" and r["status"] == "kept"
    ]
    rows: list[TextFoolerSample] = []
    for r in kept_mal:
        rows.append(
            _variant_row(
                sources[r["sample_id"]],
                r,
                runner.benign,
                victim,
                runner.judge.name,
                "textfooler",
                "tf-",
            )
        )
    main = [r for r in kept_mal if r["attack_group"] != runner.tf.translation_group]
    counts: dict[str, Any] = {"malicious_kept": Counter(r["scenario"] for r in kept_mal)}
    # controls: the benign twins of the kept (main) positives, copied unchanged
    controls = [pairs_by_id[r["pair_id"]]["neg"] for r in main]
    saved = {
        r["sample_id"]: r["p_orig"]
        for r in runner.results.values()
        if r["kind"] == "twin" and r.get("p_orig") is not None
    }
    missing = [n for n in controls if n["sample_id"] not in saved]
    if missing:  # a twin never attempted: score it now (reloads the victim once)
        for n, p in zip(
            missing,
            runner.engine.score([(n["instruct"], n["query"], n["document"]) for n in missing]),
            strict=True,
        ):
            saved[n["sample_id"]] = p
        release(runner.engine)
    ps = [saved[n["sample_id"]] for n in controls]
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
                sources[r["sample_id"]],
                r,
                runner.benign,
                victim,
                runner.judge.name,
                "textfooler",
                "tfb-",
            )
        )
    counts |= {"control": len(controls), "perturbed_twins": len(chosen), "twin_cap": twin_cap}
    counts["control_p"] = control_p
    return rows, counts


# ---------------------------------------------------------------- entry point


def build_models(cfg: Config) -> tuple[Engine, Judge]:
    scorer = CachedScorer(ShieldstralScorer(cfg.textfooler))
    return TextFoolerEngine(cfg.textfooler, scorer), build_judge(cfg.textfooler.judge)


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
        runner.work(  # one round: positives and their twins together
            [
                {"kind": kind, "row": p[key], "pair_id": p["pair_id"], "doc": p["doc"]}
                for kind, key in (("malicious", "pos"), ("twin", "neg"))
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
