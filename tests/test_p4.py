import json
from collections import Counter

import pytest
from test_p2 import N_PER_DOC, _attacks, _docs

from dataset_builder.io import OutputExistsError, read_jsonl, read_parquet
from dataset_builder.schema import COLUMNS
from dataset_builder.steps import p2_bipia_positives as p2
from dataset_builder.steps import p4_bipia_assemble as p4
from dataset_builder.steps.p1_bipia_split import ATTACKS_FILE, DOCS_FILE

WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
WORDS += ["hotel", "india", "juliet", "kilo", "lima", "mike"]


def _pool(docs, lengths=None):
    """20 benign texts per (doc, position), lengths spread from short to long."""
    out = []
    for d in docs:
        for pos in ("start", "middle", "end"):
            for i in range(20):
                target = lengths or (8 + 6 * i for i in range(20))
                target = list(target)[i]
                words, text = [], ""
                while len(text) < target:
                    words.append(WORDS[(i + len(words)) % len(WORDS)])
                    text = " ".join(words).capitalize() + "."
                out.append(
                    {
                        "benign_id": f"benign-{d['doc_id']}-{pos}-{i:02d}",
                        "doc_id": d["doc_id"],
                        "scenario": d["scenario"],
                        "split": d["split"],
                        "position": pos,
                        "text": text,
                        "length": len(text),
                        "shieldstral_checked": False,
                    }
                )
    return out


@pytest.fixture
def p4cfg(cfg):
    c = cfg.model_copy(update={"expected_counts": {}})
    p3 = c.p3.model_copy(update={"n": 20})
    c = c.model_copy(
        update={"p3": p3, "bipia": c.bipia.model_copy(update={"n_per_doc": N_PER_DOC})}
    )
    interim = c.paths.interim
    interim.mkdir(parents=True)
    docs, attacks = _docs(), _attacks()
    (interim / DOCS_FILE).write_text("\n".join(json.dumps(d) for d in docs), "utf-8")
    (interim / ATTACKS_FILE).write_text("\n".join(json.dumps(a) for a in attacks), "utf-8")
    p2.run(c, n_per_doc=N_PER_DOC)
    (interim / p4.POOL_FILE).write_text("\n".join(json.dumps(t) for t in _pool(docs)), "utf-8")
    return c


def test_pick_window_least_used_and_fallback():
    cands = [
        {"benign_id": "a", "length": 50},
        {"benign_id": "b", "length": 55},
        {"benign_id": "c", "length": 200},
    ]
    uses = Counter()
    first, ok = p4.pick(52, cands, uses, 0.3)
    assert ok and first["benign_id"] == "a"  # tie on uses (0): closest length
    uses["a"] += 1
    second, ok = p4.pick(52, cands, uses, 0.3)
    assert ok and second["benign_id"] == "b"  # least used first, within the window
    far, ok = p4.pick(400, cands, uses, 0.3)
    assert not ok and far["benign_id"] == "c"  # no text in window: closest length, flagged
    uses["b"] += 1
    unused, ok = p4.pick(52, cands, uses, 0.3)
    # Least used first (option B): the unused text wins over the in-window but used ones.
    assert unused["benign_id"] == "c" and not ok


def test_run_end_to_end(p4cfg):
    out = p4.run(p4cfg)
    rows = {s: read_parquet(out["paths"][s]) for s in ("train", "validation", "test")}
    for rs in rows.values():
        assert set(rs[0]) == set(COLUMNS)  # contract columns only
        assert Counter(r["label"] for r in rs)[0] == Counter(r["label"] for r in rs)[1]
        by_pair = {}
        for r in rs:
            by_pair.setdefault(r["parent_id"], []).append(r)
        for pair in by_pair.values():
            pos, neg = sorted(pair, key=lambda r: -r["label"])
            assert (pos["label"], neg["label"]) == (1, 0)
            for k in ("query", "instruct", "position", "insert_index", "scenario", "split"):
                assert pos[k] == neg[k], k
            assert neg["generation_method"] == "llm_benign_insertion" and neg["answer"] == "no"
            assert neg["benign_id"] and neg["attack_name"] is None
            assert neg["sample_id"].removeprefix("bipia-neg-") == pos["sample_id"].removeprefix(
                "bipia-pos-"
            )
    pairs = list(read_jsonl(p4cfg.paths.interim / p4.PAIRS_FILE))
    assert len(pairs) == sum(len(v) for v in rows.values()) // 2
    md = out["report"].read_text("utf-8")
    assert "FAILED" not in md and "Length: positive vs negative" in md
    assert "Pool text reuse" in md and "Notes from P3" in md

    first = out["paths"]["test"].read_bytes()
    with pytest.raises(OutputExistsError):
        p4.run(p4cfg)  # data/out is never overwritten without --force
    out2 = p4.run(p4cfg, force=True)
    assert out2["paths"]["test"].read_bytes() == first  # deterministic


def test_fallback_flagged(p4cfg):
    # Only very long pool texts: no text is within 30% of the short attacks.
    long_pool = _pool(_docs(), lengths=[400 + i for i in range(20)])
    (p4cfg.paths.interim / p4.POOL_FILE).write_text(
        "\n".join(json.dumps(t) for t in long_pool), "utf-8"
    )
    out = p4.run(p4cfg)
    pairs = list(read_jsonl(p4cfg.paths.interim / p4.PAIRS_FILE))
    assert out["fallback"] > 0 and any(not p["length_matched"] for p in pairs)


def test_attack_text_in_pool_stops(p4cfg):
    pool = list(read_jsonl(p4cfg.paths.interim / p4.POOL_FILE))
    attack = next(a for a in _attacks() if a["group"] == "text" and a["source_split"] == "test")
    for t in pool:  # plant the attack in every pool text
        t["text"] = f"Note: {attack['text']} here."
        t["length"] = len(t["text"])
    (p4cfg.paths.interim / p4.POOL_FILE).write_text("\n".join(json.dumps(t) for t in pool), "utf-8")
    with pytest.raises(p4.P4CheckError, match="BIPIA attack text"):
        p4.run(p4cfg)
    assert not p4.out_path(p4cfg, "test").exists()
