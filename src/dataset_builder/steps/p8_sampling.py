"""P8 sampling: which bipia train / validation pairs get a training variant.

A pair is a positive and its benign twin; both get one variant each (one variant per source row).
The order per scenario is P6's stratified order (attack group x position, seeded) on its own seed
stream, with no cap per document, and the Language Translation group left out as in the test set.
Email and code take the pairs the config gives them, tables take the rest, so tables do not drown
the smaller scenarios. The sampled ids are stored.
"""

from typing import Any

from dataset_builder.config import Config
from dataset_builder.steps.p6_sampling import SCENARIOS, make_pairs, stratified_order

SPLITS = ("train", "validation")


def split_orders(
    rows: list[dict[str, Any]], cfg: Config, split: str
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Per scenario: the sampled pairs of one split, and a note of quotas and shortfalls."""
    p8 = cfg.p8
    assert p8 is not None
    total = p8.train_pairs if split == "train" else p8.validation_pairs
    quotas = p8.scenario_pairs.get(split, {})
    pairs = make_pairs(rows)
    full = {
        s: stratified_order(pairs, cfg, s, salt=f"p8:{split}", max_per_doc=None) for s in SCENARIOS
    }
    fixed = {s: quotas[s] for s in SCENARIOS if quotas.get(s) is not None}
    rest = [s for s in SCENARIOS if s not in fixed]
    left = max(total - sum(fixed.values()), 0)
    want = dict(fixed)
    for i, s in enumerate(rest):
        want[s] = left // len(rest) + (1 if i < left % len(rest) else 0)
    out = {s: full[s][: want[s]] for s in SCENARIOS}
    note = {
        "split": split,
        "wanted": want,
        "available": {s: len(full[s]) for s in SCENARIOS},
        "taken": {s: len(out[s]) for s in SCENARIOS},
        "short": {s: want[s] - len(out[s]) for s in SCENARIOS if len(out[s]) < want[s]},
    }
    return out, note


def pilot_sample(orders: dict[str, list[dict[str, Any]]], rows: int) -> list[dict[str, Any]]:
    """About `rows` source rows (rows // 2 pairs), spread over the scenarios, taken from the head
    of the full run's order (the pilot's pairs are the full run's first pairs)."""
    n = max(rows // 2, 1)
    out: list[dict[str, Any]] = []
    i = 0
    while len(out) < n and any(i < len(orders[s]) for s in SCENARIOS):
        out += [orders[s][i] for s in SCENARIOS if i < len(orders[s])][: n - len(out)]
        i += 1
    return out


def sample_records(split: str, orders: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """What is stored in data/interim/p8_sample_ids.jsonl."""
    return [
        {
            "split": split,
            "scenario": s,
            "rank": i,
            "pair_id": p["pair_id"],
            "pos_id": p["pos"]["sample_id"],
            "neg_id": p["neg"]["sample_id"],
            "attack_group": p["attack_group"],
            "position": p["position"],
        }
        for s, order in orders.items()
        for i, p in enumerate(order)
    ]
