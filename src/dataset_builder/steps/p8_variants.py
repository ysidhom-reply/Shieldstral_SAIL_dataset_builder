"""P8: TextFooler-style training variants written by a local LLM (dataset version v2).

Per run:
1. pairs (a positive and its benign twin) of bipia train and validation are sampled, stratified
   by scenario, attack group and position (Language Translation left out); ids stored;
2. generate (Qwen3-14B, not Mistral): one rewrite of the editable span per source row, then the
   rule filters (word cap, word count, protected tokens, code block); one retry on a failure;
3. judge (gpt-oss-20b, not the generator): the label on the original first, then the variant;
4. score with base Shieldstral: p_yes_base and whether it gets the variant wrong (information
   only: every variant the judge accepts is kept);
5. classes balanced per split; nested training subsets of 500 / 1,000 / 2,000 / 4,000 with equal
   classes; data/out/bipia_textfooler/{train,validation}.parquet, report, human-check CSV.

One model on the GPU at a time: each phase runs over the whole batch, saving every outcome to
data/interim/p8_results[_pilot].jsonl, then frees the GPU. A stopped run resumes where it was.
Models are given to `run` (tests pass fakes); by default they are built from the config.
"""

import logging
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dataset_builder.config import Config, load_config
from dataset_builder.io import (
    prepare_out,
    read_jsonl,
    read_parquet,
    rng,
    write_jsonl,
    write_parquet,
)
from dataset_builder.reports import CountMismatchError
from dataset_builder.schema import TextFoolerSample
from dataset_builder.steps import p6_report, p8_report
from dataset_builder.steps.p6_judge import PROMPT_VERSION, Judge, build_judge
from dataset_builder.steps.p6_sampling import SCENARIOS
from dataset_builder.steps.p6_span import (
    WORD,
    locate_span,
    modifiable_indices,
    nltk_entities,
    protected_tokens,
)
from dataset_builder.steps.p6_textfooler import (
    _variant_row,
    append_results,
    load_results,
    modified_text,
    release,
)
from dataset_builder.steps.p6_victim import CachedScorer, Scorer, ShieldstralScorer
from dataset_builder.steps.p8_generator import Generator, QwenGenerator, RewriteItem
from dataset_builder.steps.p8_rules import check_llm_variant, fit_whitespace
from dataset_builder.steps.p8_sampling import SPLITS, pilot_sample, sample_records, split_orders

logger = logging.getLogger(__name__)

STATUSES = (
    "no_editable",  # no word of the span may be changed (all protected)
    "rule_rejected",  # the rewrite broke a rule (after one retry)
    "pending_judge",  # waiting for the judge phase
    "judge_disagrees",  # the judge disagrees with the label on the original (or no answer)
    "judge_rejected",  # the judge says the meaning or the label changed
    "kept",  # accepted: a training variant
)
CHUNK = 200  # candidates per saved chunk in each phase


class PassRateError(CountMismatchError):
    """Fewer variants than `min_pass_rate` passed the rules and the judge."""


# ---------------------------------------------------------------- phases


def make_record(it: dict[str, Any], benign: dict[str, str], entities: Any) -> tuple[dict, Any]:
    row = it["row"]
    span = locate_span(row, benign.get(row.get("benign_id") or ""))
    tokens = protected_tokens(span.text, entities)
    rec = {
        "key": f"{row['split']}:{row['sample_id']}",
        "sample_id": row["sample_id"],
        "pair_id": it["pair_id"],
        "split": row["split"],
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
    return rec, span


def generate_phase(
    items: list[dict[str, Any]],
    generator: Generator,
    cfg: Config,
    benign: dict[str, str],
    entities: Any,
) -> list[dict[str, Any]]:
    """Rewrite every item's span, apply the rules, retry the failures once."""
    assert cfg.p8 is not None
    cap, tol = cfg.textfooler.max_span_fraction, cfg.p8.word_count_tolerance
    recs, live = [], []
    for it in items:
        rec, span = make_record(it, benign, entities)
        recs.append(rec)
        if rec["n_words"] == 0 or not modifiable_indices(
            WORD.findall(span.text), rec["protected_tokens"]
        ):
            rec["status"] = "no_editable"
            continue
        item = RewriteItem(
            text=span.text,
            label=rec["label"],
            protected=tuple(rec["protected_tokens"]),
            max_changed=max(1, math.floor(cap * rec["n_words"])),
            n_words=rec["n_words"],
        )
        live.append((rec, span, item))

    def attempt(
        batch: list[tuple[dict, Any, RewriteItem]], n: int
    ) -> list[tuple[dict, Any, RewriteItem]]:
        t0 = time.monotonic()
        outs = generator.rewrite([x[2] for x in batch])
        share = (time.monotonic() - t0) / max(len(batch), 1)
        failed = []
        for (rec, span, item), out in zip(batch, outs, strict=True):
            new = fit_whitespace(span.text, out)
            reason, changed, _ = check_llm_variant(span, new, rec["protected_tokens"], cap, tol)
            rec["seconds"] += share
            rec |= {"new_span": new, "attempts": n, "words_changed": changed}
            if reason:
                rec["status"], rec["reason"] = "rule_rejected", reason
                failed.append((rec, span, item))
            else:
                rec["status"] = "pending_judge"
                rec.pop("reason", None)
        return failed

    retry = attempt(live, 1)
    if retry:
        for rec, _, _ in retry:
            rec["first_reason"] = rec["reason"]
        attempt(retry, 2)
    for rec in recs:
        rec["seconds"] = round(rec["seconds"], 3)
    return recs


def score_phase(
    recs: list[dict[str, Any]],
    scorer: Scorer,
    sources: dict[str, dict[str, Any]],
    benign: dict[str, str],
    threshold: float,
) -> list[dict[str, Any]]:
    """Base Shieldstral's p(yes) on every accepted variant (whole document, as in use)."""
    items = []
    for r in recs:
        row = sources[r["sample_id"]]
        span = locate_span(row, benign.get(row.get("benign_id") or ""))
        items.append((row["instruct"], row["query"], span.document(r["new_span"])))
    for r, p in zip(recs, scorer.score(items) if items else [], strict=True):
        r["p_yes_base"] = p
        r["wrong"] = p < threshold if r["label"] == 1 else p >= threshold
    return recs


def judge_phase(
    recs: list[dict[str, Any]], judge: Judge, cache: dict[tuple[str, int], dict | None]
) -> list[dict[str, Any]]:
    """The judge on `pending_judge` records: the label on the original first (each distinct
    original is judged once: BIPIA reuses its attack texts, `cache` keeps the verdicts), then the
    variant. Same outcomes as P6's judge phase."""
    if not recs:
        return recs
    first: dict[tuple[str, int], str] = {}  # distinct (text, label) not yet judged -> scenario
    for r in recs:
        key = (r["inserted"], r["label"])
        if key not in cache:
            first.setdefault(key, r["scenario"])
    new_originals = [(t, lab, sc) for (t, lab), sc in first.items()]
    queries = [(t, t, lab, sc) for t, lab, sc in new_originals]
    queries += [(r["inserted"], modified_text(r), r["label"], r["scenario"]) for r in recs]
    t0 = time.monotonic()
    vs = judge.verdicts(queries)
    share = (time.monotonic() - t0) / len(recs)
    for (t, lab, _), v in zip(new_originals, vs, strict=False):
        cache[(t, lab)] = v.as_dict() if v else None
    for r, v in zip(recs, vs[len(new_originals) :], strict=True):
        v_orig = cache[(r["inserted"], r["label"])]
        r["original_verdict"] = v_orig
        r["verdict"] = v.as_dict() if v else None
        if v_orig is None or not v_orig["label_still_correct"]:
            r["status"] = "judge_disagrees"
        else:
            r["status"] = "kept" if v is not None and v.accepted else "judge_rejected"
        r["seconds"] = round(r["seconds"] + share, 3)
        r["judge_prompt"] = PROMPT_VERSION
    return recs


def original_cache(results: dict[str, dict[str, Any]]) -> dict[tuple[str, int], dict | None]:
    """The original-label verdicts already in the results file (a resumed run reuses them), from
    the current judge prompt only."""
    return {
        (r["inserted"], r["label"]): r["original_verdict"]
        for r in results.values()
        if "original_verdict" in r and r.get("judge_prompt") == PROMPT_VERSION
    }


JUDGED = ("kept", "judge_rejected", "judge_disagrees")
JUDGE_FIELDS = ("verdict", "original_verdict", "judge_prompt", "p_yes_base", "wrong")


def reset_judged(results: dict[str, dict[str, Any]], path: Path) -> int:
    """Send every judged row back to `pending_judge` (the rewrite is kept) so the whole set is
    judged with the current prompt. Appended to the results file: the last line of a key wins."""
    reset = []
    for r in results.values():
        if r["status"] in JUDGED and r.get("judge_prompt") != PROMPT_VERSION:
            r = {k: v for k, v in r.items() if k not in JUDGE_FIELDS} | {"status": "pending_judge"}
            reset.append(r)
    for r in reset:
        results[r["key"]] = r
    append_results(path, reset)
    return len(reset)


# ---------------------------------------------------------------- runner


class Runner:
    def __init__(
        self,
        cfg: Config,
        generator: Generator,
        judge: Judge,
        scorer: Scorer,
        sources: dict[str, dict[str, Any]],
        benign: dict[str, str],
        entities: Any,
        results: dict[str, dict[str, Any]],
        path: Path,
    ) -> None:
        self.cfg = cfg
        self.generator, self.judge, self.scorer = generator, judge, scorer
        self.sources, self.benign, self.entities = sources, benign, entities
        self.results, self.path = results, path
        self.times: dict[str, float] = {"generate": 0.0, "judge": 0.0, "score": 0.0}
        self.cache = original_cache(results)

    def _save(self, recs: list[dict[str, Any]]) -> None:
        for r in recs:
            self.results[r["key"]] = r
        append_results(self.path, recs)

    def run(self, items: list[dict[str, Any]]) -> None:
        """The three phases over `items`; what is already saved is not redone."""
        todo = [
            it
            for it in items
            if f"{it['row']['split']}:{it['row']['sample_id']}" not in self.results
        ]
        t0 = time.monotonic()
        for i in range(0, len(todo), CHUNK):
            self._save(
                generate_phase(
                    todo[i : i + CHUNK], self.generator, self.cfg, self.benign, self.entities
                )
            )
            logger.info("P8 generate: %d / %d", min(i + CHUNK, len(todo)), len(todo))
        release(self.generator)
        self.times["generate"] += time.monotonic() - t0

        t0 = time.monotonic()
        pending = [r for r in self.results.values() if r["status"] == "pending_judge"]
        for i in range(0, len(pending), CHUNK):
            self._save(judge_phase(pending[i : i + CHUNK], self.judge, self.cache))
            logger.info("P8 judge: %d / %d", min(i + CHUNK, len(pending)), len(pending))
        release(self.judge)
        self.times["judge"] += time.monotonic() - t0

        t0 = time.monotonic()
        unscored = [
            r
            for r in self.results.values()
            if r["status"] == "kept" and r.get("p_yes_base") is None
        ]
        thr = self.cfg.textfooler.threshold
        for i in range(0, len(unscored), CHUNK):
            self._save(
                score_phase(unscored[i : i + CHUNK], self.scorer, self.sources, self.benign, thr)
            )
        release(self.scorer)
        self.times["score"] += time.monotonic() - t0


# ---------------------------------------------------------------- assembly


def balance_and_nest(
    kept: list[dict[str, Any]], cfg: Config, split: str
) -> tuple[list[tuple[dict[str, Any], int | None]], dict[str, Any]]:
    """Equal classes (the smaller class sets the size; variants whose twin was also kept come
    first, then a seeded order), and on train the nested subsets: the first k/2 of each class
    form subset k, so 500 sits inside 1,000, 1,000 inside 2,000 and 2,000 inside 4,000."""
    assert cfg.p8 is not None
    by_label = {lab: [r for r in kept if r["label"] == lab] for lab in (1, 0)}
    pairs_both = {
        p for p in {r["pair_id"] for r in kept} if sum(r["pair_id"] == p for r in kept) == 2
    }
    for lab, rs in by_label.items():
        rs.sort(key=lambda r: r["key"])
        rng(f"p8:assemble:{split}:{lab}", cfg).shuffle(rs)
        rs.sort(key=lambda r: r["pair_id"] not in pairs_both)  # stable: complete pairs first
    n = min(len(by_label[1]), len(by_label[0]))
    sizes = sorted(cfg.p8.nested_sizes) if split == "train" else []
    formed = [k for k in sizes if n >= k // 2]  # a subset exists only if both halves are full
    out: list[tuple[dict[str, Any], int | None]] = []
    for lab in (1, 0):
        for i, r in enumerate(by_label[lab][:n]):
            nested = next((k for k in formed if i < k // 2), None)
            out.append((r, nested))
    note = {
        "kept": {str(lab): len(rs) for lab, rs in by_label.items()},
        "per_class": n,
        "dropped_for_balance": {str(lab): len(rs) - n for lab, rs in by_label.items()},
        "nested": {str(k): (k if n >= k // 2 else None) for k in sizes},
        "short": [k for k in sizes if n < k // 2],
    }
    return out, note


def assemble(
    runner: Runner, split: str, generator_name: str, judge_name: str
) -> tuple[list[TextFoolerSample], dict[str, Any]]:
    kept = [r for r in runner.results.values() if r["split"] == split and r["status"] == "kept"]
    chosen, note = balance_and_nest(kept, runner.cfg, split)
    rows = []
    for r, nested in chosen:
        src = runner.sources[r["sample_id"]]
        rows.append(
            _variant_row(
                src,
                r | {"p_new": r["p_yes_base"]},
                runner.benign,
                None,
                judge_name,
                "llm_textfooler_style",
                "p8-",
                extra={"generator_model": generator_name, "nested_size": nested},
            )
        )
    return rows, note


# ---------------------------------------------------------------- entry point


def build_models(cfg: Config) -> tuple[Generator, Judge, Scorer]:
    assert cfg.p8 is not None
    return (
        QwenGenerator(cfg.p8.generator, cfg.seed),
        build_judge(cfg.p8.judge),
        CachedScorer(ShieldstralScorer(cfg.textfooler)),
    )


def items_of(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One item per source row: the positive and the negative of each pair."""
    return [{"row": p[key], "pair_id": p["pair_id"]} for p in pairs for key in ("pos", "neg")]


def pass_rate(results: list[dict[str, Any]]) -> float | None:
    done = [r for r in results if r["status"] != "pending_judge"]
    return sum(r["status"] == "kept" for r in done) / len(done) if done else None


def run(
    cfg: Config | None = None,
    *,
    force: bool = False,
    is_pilot: bool = True,
    generator: Generator | None = None,
    judge: Judge | None = None,
    scorer: Scorer | None = None,
    entities: Callable[[str], list[str]] | bool | None = True,
    rejudge: bool = False,
) -> dict[str, Any]:
    """`rejudge`: rows judged with an older judge prompt are judged again (rewrites kept)."""
    cfg = cfg or load_config()
    if cfg.p8 is None:
        raise ValueError("configs/config.yaml has no `p8:` block")
    p8 = cfg.p8
    t0 = time.monotonic()
    rows = {s: read_parquet(cfg.paths.out / "bipia" / f"{s}.parquet") for s in SPLITS}
    sources = {r["sample_id"]: r for s in SPLITS for r in rows[s]}
    orders, notes = {}, {}
    for s in SPLITS:
        orders[s], notes[s] = split_orders(rows[s], cfg, s)
    needed = {r["benign_id"] for r in sources.values() if r.get("benign_id")}
    pool = cfg.paths.interim / "benign_pool.jsonl"
    benign = (
        {b["benign_id"]: b["text"] for b in read_jsonl(pool) if b["benign_id"] in needed}
        if pool.is_file()
        else {}
    )
    ents = nltk_entities(cfg) if entities is True else (entities or None)
    if generator is None or judge is None or scorer is None:
        g, j, sc = build_models(cfg)
        generator, judge, scorer = generator or g, judge or j, scorer or sc
    path = cfg.paths.interim / ("p8_results_pilot.jsonl" if is_pilot else "p8_results.jsonl")
    results = load_results(path)
    if results:
        logger.info("P8: resuming, %d rows already processed", len(results))
    if rejudge:
        logger.info(
            "P8: %d rows sent back to the judge (%s)", reset_judged(results, path), PROMPT_VERSION
        )
    stale = [
        r["key"]
        for r in results.values()
        if r["status"] in JUDGED and r.get("judge_prompt") != PROMPT_VERSION
    ]
    if stale and not is_pilot:
        raise ValueError(
            f"P8: {len(stale)} rows were judged with an older judge prompt; run with --rejudge "
            f"so every row is judged with {PROMPT_VERSION}"
        )
    runner = Runner(cfg, generator, judge, scorer, sources, benign, ents, results, path)

    if is_pilot:
        sample = pilot_sample(orders["train"], p8.pilot_rows)
        write_jsonl(
            cfg.paths.interim / "p8_sample_ids_pilot.jsonl",
            sample_records(
                "train", {s: [p for p in sample if p["scenario"] == s] for s in SCENARIOS}
            ),
        )
        runner.run(items_of(sample))
        wanted = {it["row"]["sample_id"] for it in items_of(sample)}
        res = [r for r in results.values() if r["sample_id"] in wanted]
        info = generator.info() | judge.info() | scorer.info()
        out = p8_report.write(
            cfg, res, notes, info, runner.times, time.monotonic() - t0, pilot=True
        )
        return out | {"results": path, "pass_rate": pass_rate(res)}

    write_jsonl(
        cfg.paths.interim / "p8_sample_ids.jsonl",
        [rec for s in SPLITS for rec in sample_records(s, orders[s])],
    )
    items = [it for s in SPLITS for it in items_of([p for sc in SCENARIOS for p in orders[s][sc]])]
    runner.run(items)
    res = list(results.values())
    info = generator.info() | judge.info() | scorer.info()
    rate = pass_rate(res)
    if rate is not None and rate < p8.min_pass_rate:
        report = p8_report.write(cfg, res, notes, info, runner.times, time.monotonic() - t0)
        raise PassRateError(
            f"P8: only {rate:.1%} of the variants passed the rules and the judge (minimum "
            f"{p8.min_pass_rate:.0%}). Nothing written to data/out; see {report['md']}"
        )
    built, assembly = {}, {}
    for s in SPLITS:
        built[s], assembly[s] = assemble(runner, s, generator.name, judge.name)
        ids = [x.sample_id for x in built[s]]
        if len(set(ids)) != len(ids):
            raise CountMismatchError(f"P8: duplicate sample_id in {s}")
        if not built[s]:
            raise CountMismatchError(f"P8: nothing kept for {s}")
    outs = {}
    for s in SPLITS:
        outs[s] = prepare_out(
            cfg.paths.out / "bipia_textfooler" / f"{s}.parquet", force=force, cfg=cfg
        )
    for s in SPLITS:
        write_parquet(outs[s], built[s])
    report = p8_report.write(
        cfg, res, notes, info, runner.times, time.monotonic() - t0, assembly=assembly, built=built
    )
    human = p6_report.write_human_check(
        cfg, [r for r in res if r["split"] == "train"], sources, n=p8.human_check_n, step="p8"
    )
    return {"out": outs, "results": path, "report": report, "human_check": human, "pass_rate": rate}
