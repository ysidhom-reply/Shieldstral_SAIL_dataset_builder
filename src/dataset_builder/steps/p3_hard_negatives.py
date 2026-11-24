"""P3, option B: replace part of every benign pool with hard negatives.

Hard negatives are harmless texts that sound like instructions to the human reader of the
document ("Please send your reply by Friday", "You can add the following snippet to your
script ..."), using the vocabulary the attacks use. Team decision (2026-11-24) after P7 v0: the
brief's P3 filters keep that vocabulary out of every benign text, so a TF-IDF baseline separated
code at AUC 0.967 on wording alone.

For every pool, `share` of its texts (those with the most over-represented words of P7's
diagnostic) are replaced by new texts of the same sizes, generated with prompts A-hn / B-hn,
checked with a narrower filter profile and judged with judge-v4. The other texts stay as they
are. Pool sizes never change: if a pool cannot get all its new texts, the best of its replaced
texts are put back (counted in the report).

The original pool is kept as data/interim/benign_pool_v1.jsonl, and every run starts from it.
"""

import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from typing import Any

from dataset_builder.benign_filters import BUCKETS, AttackIndex
from dataset_builder.config import Config, load_config
from dataset_builder.io import read_jsonl, rng, write_jsonl
from dataset_builder.mistral_client import MistralClient
from dataset_builder.reports import report_path, write_counts_report
from dataset_builder.steps.p1_bipia_split import ATTACKS_FILE, DOCS_FILE
from dataset_builder.steps.p3_benign_pool import (
    P3CheckError,
    Pool,
    RunState,
    _table,
    generate_round,
    handread,
    judge_control_check,
    judge_round,
    load_prompts,
    rejection_tables,
    shieldstral_available,
    stratified_pilot,
    top_up,
)

logger = logging.getLogger(__name__)

STEP = "p3_hard_negatives"
POOL_FILE = "benign_pool.jsonl"
V1_FILE = "benign_pool_v1.jsonl"
# Vocabulary of the attacks that the brief's filters kept out of benign texts (P7 v0).
MARKERS = {
    "your reply/answer/response": re.compile(r"\byour (reply|answer|response)\b", re.I),
    "please / make sure / let me know": re.compile(r"\b(please|make sure|let me know)\b", re.I),
    "the following code/snippet": re.compile(r"\b(following|below) (code|snippet)\b", re.I),
    "your code/script/project": re.compile(r"\byour (code|script|project|codebase)\b", re.I),
    "requests": re.compile(r"\brequests\b"),
    "subprocess": re.compile(r"\bsubprocess\b"),
    "example.com": re.compile(r"\bexample\.(com|org)\b", re.I),
}


def hn_prompts(prompts: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """The P3 prompts with A-hn, B-hn and judge-v4 in place of A, B and judge-v3."""
    hn = cfg.p3.hard_negatives
    return prompts | {
        "placeholders": prompts["placeholders_hn"],
        "prompt_a": prompts["prompt_a_hn"],
        "prompt_b": prompts["prompt_b_hn"],
        "judge": prompts["judge_v4"],
        "judge_batch": prompts["judge_batch_v2"],
        "prompt_version": hn.prompt_version,
        "judge_prompt_version": hn.judge_prompt_version,
        "judge_batch_prompt_version": hn.judge_batch_prompt_version,
    }


def hn_config(cfg: Config) -> Config:
    """The config with the hard-negative filter profile (only for the new texts)."""
    hn = cfg.p3.hard_negatives
    p3 = cfg.p3.model_copy(
        update={
            "assistant_words": hn.assistant_words,
            "assistant_words_text_only": hn.assistant_words_text_only,
            "code_intro_patterns": [],
            "code_forbidden_patterns": hn.code_forbidden_patterns,
            "allowed_url_hosts": hn.allowed_url_hosts,
        }
    )
    return cfg.model_copy(update={"p3": p3})


def n_replace(n: int, share: float) -> int:
    return int(n * share + 0.5)


def replace_score(text: str, words: set[str]) -> int:
    return len(set(re.findall(r"[a-z]+", text.lower())) & words)


def choose_replacements(rows: list[dict], k: int, words: set[str], key: str, cfg: Config) -> list:
    """Indices of the k texts to replace: most over-represented words first, ties seeded."""
    r = rng(f"p3hn:{key}", cfg)
    tie = [r.random() for _ in rows]
    order = sorted(range(len(rows)), key=lambda i: (-replace_score(rows[i]["text"], words), tie[i]))
    return order[:k]


def plan_pools(
    groups: dict[tuple[str, str], list[dict]], docs: dict[str, dict], cfg: Config
) -> list[tuple[Pool, list[dict], list[dict]]]:
    """(new-text pool, kept rows, replaced rows) per (doc_id, position)."""
    hn = cfg.p3.hard_negatives
    words = {w.lower() for w in hn.replace_words}
    out = []
    for (doc_id, position), rows in groups.items():
        k = n_replace(len(rows), hn.share)
        drop = choose_replacements(rows, k, words, f"{doc_id}|{position}", cfg)
        dropped = [rows[i] for i in drop]
        kept = [r for i, r in enumerate(rows) if i not in set(drop)]
        mix = Counter(r["length_bucket"] for r in dropped)
        pool = Pool(
            docs[doc_id],
            position,
            target=k,
            mix={b: mix.get(b, 0) for b in BUCKETS},
            prior=[r["text"] for r in kept],
        )
        out.append((pool, kept, dropped))
    return out


def merge(
    planned: list[tuple[Pool, list[dict], list[dict]]],
) -> tuple[list[dict], list[dict], dict[str, int]]:
    """Final pool rows, the replaced rows that left the pool, and counts. A pool short of new
    texts gets back its replaced texts with the fewest over-represented words (last chosen)."""
    final, replaced = [], []
    counts = Counter()
    for pool, kept, dropped in planned:
        new = [a | {"style": "hard_negative"} for a in pool.accepted]
        short = len(dropped) - len(new)
        restored = dropped[len(dropped) - short :] if short > 0 else []
        gone = dropped[: len(dropped) - short] if short > 0 else dropped
        final += [r | {"style": r.get("style", "standard")} for r in kept + restored] + new
        replaced += gone
        counts["new"] += len(new)
        counts["restored"] += len(restored)
        counts["replaced"] += len(gone)
        counts["pools_short"] += int(short > 0)
    return final, replaced, dict(counts)


def marker_table(rows: list[dict]) -> str:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["scenario"]].append(r)
    header = ["scenario", "texts", *MARKERS]
    body = []
    for s in ("email", "table", "code"):
        rs = by.get(s, [])
        if rs:
            body.append(
                [s, len(rs)]
                + [
                    f"{sum(bool(rx.search(r['text'])) for r in rs) / len(rs):.0%}"
                    for rx in MARKERS.values()
                ]
            )
    return _table(header, body)


def run(
    cfg: Config | None = None,
    is_pilot: bool = True,
    client: MistralClient | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    p3, hn = cfg.p3, cfg.p3.hard_negatives
    interim = cfg.paths.interim
    sfx = "_pilot" if is_pilot else ""
    pool_path, v1_path = interim / POOL_FILE, interim / V1_FILE
    source = v1_path if v1_path.is_file() else pool_path
    rows = list(read_jsonl(source))
    if not v1_path.is_file() and any(r.get("style") == "hard_negative" for r in rows):
        raise P3CheckError(f"{pool_path} already has hard negatives but {v1_path} is missing")

    docs = {d["doc_id"]: d for d in read_jsonl(interim / DOCS_FILE)}
    attacks = list(read_jsonl(interim / ATTACKS_FILE))
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["doc_id"], r["position"])].append(r)
    if is_pilot:
        keep = {d["doc_id"] for d in stratified_pilot(list(docs.values()), cfg.pilot_size, cfg)}
        groups = {k: v for k, v in groups.items() if k[0] in keep}

    prompts = hn_prompts(load_prompts(cfg), cfg)
    cfg_hn = hn_config(cfg)
    client = client or MistralClient(interim / "p3_cache", max_retries=p3.max_retries)
    gen_model = client.resolve_model(p3.generator.model)
    judge = client.resolve_model(cfg.benign_check.judge_model)
    label_checked, _ = shieldstral_available(cfg)

    state = RunState(attacks=AttackIndex.build(attacks, p3.attack_overlap_words))
    planned = plan_pools(groups, docs, cfg)
    pools = [pl for pl, _, _ in planned]
    logger.info(
        "P3 hard negatives: %d pools, %d texts to replace",
        len(pools),
        sum(pl.target for pl in pools),
    )
    for k in range(1, hn.max_rounds + 2):
        todo = [pl for pl in pools if len(pl.accepted) < pl.target]
        if not todo:
            break
        label = f"hn{k}"
        logger.info("P3 hard negatives round %d: %d pools need texts", k, len(todo))
        cands = generate_round(todo, label, client, prompts, cfg_hn, state)
        judge_round(cands, client, judge["id"], prompts, cfg_hn, state, label_checked, label)
    if p3.top_up_from_spares:
        top_up(pools, client, judge["id"], prompts, cfg_hn, state, label_checked)
    state.control = judge_control_check(client, judge["id"], attacks, prompts, cfg_hn)

    final, replaced, counts = merge(planned)
    failures = []
    sizes_before = {k: len(v) for k, v in groups.items()}
    sizes_after = Counter((r["doc_id"], r["position"]) for r in final)
    if sizes_after != Counter(sizes_before):
        failures.append("pool sizes changed")
    if len({r["benign_id"] for r in final}) != len(final):
        failures.append("benign_id is not unique")
    ctl = state.control
    if ctl["flagged"] != ctl["n"]:
        failures.append(f"judge-v4 positive control: {ctl['flagged']}/{ctl['n']} attacks flagged")

    new_rows = [r for r in final if r["style"] == "hard_negative"]
    costs = client.usage.cost(p3.prices)
    overall, per_group = rejection_tables(state, pools)
    reasons = state.reasons.most_common(15)
    sections = [
        (
            "Why",
            "Team decision (2026-11-24), option B of P7 v0: the brief's P3 filters keep the "
            "attacks' vocabulary out of every benign text, so TF-IDF separated BIPIA code at AUC "
            f"0.967. {hn.share:.0%} of every pool is replaced by harmless texts that sound like "
            "instructions to the human reader (prompts A-hn / B-hn, judge-v4).",
        ),
        (
            "Run",
            "\n".join(
                [
                    f"- Mode: {'pilot' if is_pilot else 'full'}; {len(pools):,} pools; source "
                    f"`{source.name}`.",
                    f"- Texts replaced: {counts.get('replaced', 0):,}; new hard negatives "
                    "accepted: "
                    f"{counts.get('new', 0):,}; replaced texts put back (pool short of new texts): "
                    f"{counts.get('restored', 0):,} in {counts.get('pools_short', 0)} pools.",
                    f"- Generated: {state.generated:,}. Generator `{gen_model['id']}`, judge "
                    f"`{judge['id']}` ({hn.judge_prompt_version} / "
                    f"{hn.judge_batch_prompt_version}).",
                    f"- Cost: ${sum(costs.values()):.4f} ("
                    + ", ".join(f"{m} ${c:.4f}" for m, c in costs.items())
                    + ").",
                ]
            ),
        ),
        ("Attack vocabulary in the new texts (share of texts)", marker_table(new_rows)),
        ("Rejection rate per filter (new texts)", overall),
        ("Rejections per scenario and position", per_group),
        (
            "Most common rejection reasons",
            _table(
                ["filter", "reason", "texts"],
                [[f, r.replace("|", "\\|"), n] for (f, r), n in reasons],
            ),
        ),
        (
            "Judge-v4 positive control",
            f"{ctl['flagged']} of {ctl['n']} real BIPIA attacks "
            "flagged"
            + (f"; missed: {[m['attack_id'] for m in ctl['missed']]}" if ctl["missed"] else "")
            + ". Required: all.",
        ),
        (
            "Filter profile for the new texts (deviation from the P3 brief)",
            "\n".join(
                [
                    "- Assistant wording: "
                    + ", ".join(f"`{w}`" for w in hn.assistant_words)
                    + " (`your response` and `your answer` are allowed: addressed to the reader).",
                    "- Code-intro patterns ('add the following code', 'make sure your code "
                    "includes'): "
                    "not applied; judge-v4 decides.",
                    "- Forbidden code: " + ", ".join(f"`{x}`" for x in hn.code_forbidden_patterns),
                    "- Allowed: `requests.get` with a timeout on placeholder URLs, "
                    "`subprocess.run` with "
                    "a fixed argument list, local file reads. URLs only on "
                    + ", ".join(f"`{h}`" for h in hn.allowed_url_hosts)
                    + " (RFC 2606).",
                    "- Unchanged: shape, 8-word overlap with BIPIA attacks, near-duplicates "
                    "(also against "
                    "the kept texts), length, size cap and top-up.",
                ]
            ),
        ),
        (
            "Checks",
            "\n".join(
                [f"- FAILED: {f}" for f in failures]
                or [
                    "- PASSED: every pool keeps its size",
                    "- PASSED: benign_id unique",
                    "- PASSED: judge-v4 flags every positive-control attack",
                ]
            ),
        ),
    ]
    write_counts_report(
        STEP,
        new_rows,
        keys=("scenario", "split", "position"),
        cfg=cfg,
        sections=sections,
        name=f"p3_hard_negatives{sfx}",
        extra={
            "counts": counts,
            "generated": state.generated,
            "cost_usd": costs,
            "judge_control": ctl,
            "failures": failures,
            "reach": {"|".join(map(str, k)): v for k, v in state.reach.items()},
            "rejected": {"|".join(map(str, k)): v for k, v in state.drop.items()},
        },
    )
    report = report_path(cfg, f"p3_hard_negatives{sfx}.md")
    report_path(cfg, f"p3_hard_negatives{sfx}_handread.md").write_text(
        handread(pools, cfg, 10), encoding="utf-8"
    )
    write_jsonl(interim / f"benign_rejected_hn{sfx}.jsonl", state.rejected)
    if failures:
        raise P3CheckError(f"P3 hard negatives stopped, pool not written. See {report}")
    write_jsonl(interim / f"benign_replaced{sfx}.jsonl", replaced)
    if is_pilot:
        out = write_jsonl(interim / "benign_pool_hn_pilot.jsonl", final)
        out_path = interim / "benign_pool_hn_pilot.jsonl"
    else:
        if not v1_path.is_file():
            shutil.copy2(pool_path, v1_path)  # keep the original pool once
        out = write_jsonl(pool_path, final)
        out_path = pool_path
    logger.info("Wrote %d benign texts (%d hard negatives) -> %s", out, len(new_rows), out_path)
    return {
        "pool": out_path,
        "report": report,
        "counts": counts,
        "cost_usd": sum(costs.values()),
        "control": ctl,
        "json": json.dumps(counts),
    }
