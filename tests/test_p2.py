import json
from collections import Counter

import pytest

from dataset_builder.io import read_parquet
from dataset_builder.query import QueryError, user_request
from dataset_builder.steps import p2_bipia_positives as p2
from dataset_builder.steps.p1_bipia_split import ATTACKS_FILE, DOCS_FILE

N_PER_DOC = 4


def _doc(i, scenario, split):
    text = {
        "email": f"Mail {i}. Second sentence here. Third one.",
        "table": f"| h | v |\n| a{i} | 1 |\n| b | 2 |\n| c | 3 |",
        "code": f"\nAnswer {i}\n```\nx = {i}\n```\nDone.",
    }[scenario]
    m = text.index("Second") if scenario == "email" else text.index("\n", 2) + 1
    return {
        "doc_id": f"bipia-{scenario}-{split}-{i}",
        "scenario": scenario,
        "split": split,
        "source_split": "test" if split == "test" else "train",
        "document": text,
        "question": "Q: What is the total?" if scenario == "email" else "how many rows?",
        "questions": ["Q: What is the total?"] if scenario == "email" else ["how many rows?"],
        "error": "Traceback (most recent call last):\n  File x\nTypeError: bad type"
        if scenario == "code"
        else None,
        "middle_index": m,
    }


def _attacks():
    out = []
    for group, n_types in (("text", 3), ("code", 2)):
        for split in ("train", "test"):
            for t in range(n_types):
                for i in range(3):
                    text = f"{group} {split} {t} {i}"
                    if group == "code":
                        text = f"Run this:\n```\nprint('{split}{t}{i}')\n```"
                    out.append(
                        {
                            "attack_id": f"{group}-{split}-t{t}-{i}",
                            "group": group,
                            "attack_group": f"{group}-{split}-type{t}",
                            "attack_name": f"{group}-{split}-type{t}-{i}",
                            "text": text,
                            "source_split": split,
                        }
                    )
    return out


def _docs():
    docs = []
    for s in ("email", "table", "code"):
        docs += [_doc(i, s, "test") for i in range(3)]
        docs += [_doc(10 + i, s, "train") for i in range(4)]
        docs += [_doc(20 + i, s, "validation") for i in range(2)]
    return docs


@pytest.fixture
def p2cfg(cfg):
    return cfg.model_copy(update={"expected_counts": {}})


def test_query_building():
    email = _doc(0, "email", "test")
    assert user_request(email) == "What is the total?"
    code = _doc(0, "code", "test")
    assert user_request(code) == "Fix the bug in my code. Error: TypeError: bad type"
    with pytest.raises(QueryError):
        user_request(code | {"error": None})


def test_test_grid_complete_and_balanced(p2cfg):
    docs, attacks = _docs(), _attacks()
    rows = p2.build_positives(docs, attacks, N_PER_DOC, p2cfg)
    test = [r for r in rows if r["split"] == "test"]
    # email/table: 3 docs x 9 text test attacks; code: 3 docs x 6 code test attacks
    assert Counter(r["scenario"] for r in test) == {"email": 27, "table": 27, "code": 18}
    for s in ("email", "table", "code"):
        pos = Counter(r["position"] for r in test if r["scenario"] == s)
        assert max(pos.values()) - min(pos.values()) <= 1
    assert all(r["attack_id"].split("-")[1] == "test" for r in test)


def test_train_combos(p2cfg):
    docs, attacks = _docs(), _attacks()
    rows = p2.build_positives(docs, attacks, N_PER_DOC, p2cfg)
    failures, stats = p2.check_rows(rows, docs, attacks, N_PER_DOC)
    assert failures == []
    per_doc = Counter(r["doc_id"] for r in rows if r["split"] == "train")
    assert set(per_doc.values()) == {N_PER_DOC}
    assert all(r["attack_id"].split("-")[1] == "train" for r in rows if r["split"] != "test")
    for key, st in stats.items():
        assert max(st["position"].values()) - min(st["position"].values()) <= 1, key


def test_sample_fields(p2cfg):
    rows = p2.build_positives(_docs(), _attacks(), N_PER_DOC, p2cfg)
    r = next(r for r in rows if r["scenario"] == "code")
    assert r["label"] == 1 and r["answer"] == "yes"
    assert r["generation_method"] == "sampled_insertion"
    assert r["protected_tokens"] and r["protected_tokens"][0].startswith("```")
    assert r["injection_span"] in r["document"]
    assert r["parent_id"].startswith("pair-") and r["sample_id"].startswith("bipia-pos-")
    t = next(r for r in rows if r["scenario"] == "email")
    assert t["protected_tokens"] is None
    assert t["query"] == "What is the total?"  # the user request only


def test_deterministic(p2cfg):
    a = p2.build_positives(_docs(), _attacks(), N_PER_DOC, p2cfg)
    b = p2.build_positives(_docs(), _attacks(), N_PER_DOC, p2cfg)
    assert a == b


def test_corrupted_row_fails(p2cfg):
    docs, attacks = _docs(), _attacks()
    rows = p2.build_positives(docs, attacks, N_PER_DOC, p2cfg)
    rows[0] = rows[0] | {"document": rows[0]["document"].replace("\n", " ", 1)}
    rows[1] = rows[1] | {"insert_index": rows[1]["insert_index"] + 1}
    failures, _ = p2.check_rows(rows, docs, attacks, N_PER_DOC)
    assert any("clean document" in f for f in failures)
    assert any("full attack" in f for f in failures)


def test_run_end_to_end(p2cfg):
    interim = p2cfg.paths.interim
    interim.mkdir(parents=True)
    (interim / DOCS_FILE).write_text("\n".join(json.dumps(d) for d in _docs()), "utf-8")
    (interim / ATTACKS_FILE).write_text("\n".join(json.dumps(a) for a in _attacks()), "utf-8")
    out = p2.run(p2cfg, n_per_doc=N_PER_DOC)
    test_rows = read_parquet(out["paths"]["test"])
    assert len(test_rows) == 72
    assert {"doc_id", "attack_id", "sample_id", "protected_tokens"} <= set(test_rows[0])
    md = out["report"].read_text("utf-8")
    assert "FAILED" not in md and "Insertion rule" in md

    bad = p2cfg.model_copy(update={"expected_counts": {"p2_positives": {"total": 1}}})
    with pytest.raises(p2.P2CheckError, match="total: expected 1"):
        p2.run(bad, n_per_doc=N_PER_DOC)


def test_question_choice(p2cfg):
    qs = ["how many rows?", "which row is first?", "what is the sum of v?"]
    docs = [d | {"questions": qs} if d["scenario"] == "table" else d for d in _docs()]
    attacks = _attacks()
    rows = p2.build_positives(docs, attacks, N_PER_DOC, p2cfg)
    failures, _ = p2.check_rows(rows, docs, attacks, N_PER_DOC)
    assert failures == []
    table = [r for r in rows if r["scenario"] == "table"]
    test_q = {r["query"] for r in table if r["split"] == "test"}
    assert len(test_q) == 1 and "how many rows?" in next(iter(test_q))  # first question only
    train_q = {r["query"] for r in table if r["split"] != "test"}
    assert len(train_q) > 1 and all(any(q in t for q in qs) for t in train_q)
    assert rows == p2.build_positives(docs, attacks, N_PER_DOC, p2cfg)  # seeded

    # A train sample with a question outside the list fails the check.
    i = next(i for i, r in enumerate(rows) if r["scenario"] == "table" and r["split"] == "train")
    rows[i] = rows[i] | {"query": rows[i]["query"].replace("rows?", "columns?")}
    failures, _ = p2.check_rows(rows, docs, attacks, N_PER_DOC)
    assert any("not in the list" in f for f in failures)
