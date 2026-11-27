"""P6 sampling: which test pairs get attacked, and in which order.

A pair is a positive and its benign twin (same `parent_id`, same document, same position). The
order is stratified by scenario, attack group and position, spread over all test documents with
a cap per document, pairs kept together, and fixed by the seed. The sampled ids are stored.
"""

from collections import defaultdict
from typing import Any

from dataset_builder.config import Config
from dataset_builder.io import rng
from dataset_builder.steps.p6_span import doc_key

SCENARIOS = ("email", "table", "code")


def make_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairs of (positive, negative) from test rows, joined on `parent_id`.

    Each pair: {pair_id, scenario, attack_group, position, doc, pos, neg}. A row without its
    partner is dropped (and the caller's checks report the count)."""
    by_parent: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for r in sorted(rows, key=lambda r: r["sample_id"]):
        by_parent[r["parent_id"]][int(r["label"])] = r
    pairs = []
    for parent, both in by_parent.items():
        if 0 in both and 1 in both:
            pos, neg = both[1], both[0]
            pairs.append(
                {
                    "pair_id": parent,
                    "scenario": pos["scenario"],
                    "attack_group": pos.get("attack_group") or "",
                    "position": pos.get("position") or "",
                    "doc": doc_key(pos),
                    "pos": pos,
                    "neg": neg,
                }
            )
    return pairs


def stratified_order(
    pairs: list[dict[str, Any]],
    cfg: Config,
    scenario: str,
    *,
    translation: bool = False,
    salt: str = "p6",
    max_per_doc: int | None = -1,
) -> list[dict[str, Any]]:
    """The attempt order of one scenario: round-robin over the strata (attack group x position),
    each stratum shuffled with the seed, at most `max_per_doc` pairs per document.

    `translation` selects the separate translation slice instead of the main set. `salt` names
    the seed stream (P8 uses its own); `max_per_doc` defaults to the P6 setting, None = no cap."""
    tf = cfg.textfooler
    cap = tf.max_per_doc if max_per_doc == -1 else max_per_doc
    group = tf.translation_group
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in pairs:
        if p["scenario"] != scenario or (p["attack_group"] == group) != translation:
            continue
        strata[(p["attack_group"], p["position"])].append(p)
    r = rng(f"{salt}:order:{scenario}:{'translation' if translation else 'main'}", cfg)
    for key in sorted(strata):
        strata[key].sort(key=lambda p: p["pair_id"])
        r.shuffle(strata[key])
    keys = sorted(strata)
    used: dict[str, int] = defaultdict(int)
    out: list[dict[str, Any]] = []
    pos = dict.fromkeys(keys, 0)
    progressed = True
    while progressed:
        progressed = False
        for key in keys:
            items = strata[key]
            while pos[key] < len(items):
                p = items[pos[key]]
                pos[key] += 1
                if cap is not None and used[p["doc"]] >= cap:
                    continue  # this document is full: look at the next pair of the stratum
                used[p["doc"]] += 1
                out.append(p)
                progressed = True
                break
    return out


def attempt_orders(pairs: list[dict[str, Any]], cfg: Config) -> dict[str, list[dict[str, Any]]]:
    """Per scenario: the attempt order, cut at `attempt_cap` pairs when a cap is set."""
    caps = cfg.textfooler.attempt_cap
    out = {}
    for s in SCENARIOS:
        order = stratified_order(pairs, cfg, s)
        cap = caps.get(s)
        out[s] = order[:cap] if cap else order
    return out


def pilot_pairs(pairs: list[dict[str, Any]], cfg: Config) -> list[dict[str, Any]]:
    """About `pilot_pairs` pairs spread evenly over the three scenarios, from the same order as
    the full run (so the pilot's first pairs are the full run's first pairs)."""
    n = cfg.textfooler.pilot_pairs
    orders = {s: stratified_order(pairs, cfg, s) for s in SCENARIOS}
    share = {
        s: n // len(SCENARIOS) + (1 if i < n % len(SCENARIOS) else 0)
        for i, s in enumerate(SCENARIOS)
    }
    return [p for s in SCENARIOS for p in orders[s][: share[s]]]


def sample_records(orders: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """What is stored in data/interim/p6_sample_ids.jsonl."""
    return [
        {
            "scenario": s,
            "rank": i,
            "pair_id": p["pair_id"],
            "pos_id": p["pos"]["sample_id"],
            "neg_id": p["neg"]["sample_id"],
            "attack_group": p["attack_group"],
            "position": p["position"],
            "doc": p["doc"],
        }
        for s, order in orders.items()
        for i, p in enumerate(order)
    ]
