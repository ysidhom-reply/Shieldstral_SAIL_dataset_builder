import json

import pytest

from dataset_builder.bipia_source import ATTACK_FILES
from dataset_builder.steps import p1_bipia_split as p1

TABLE = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |"
EMAIL = "SUBJECT: Hi|CONTENT: Hello there. This is the second sentence. And a third one."


def _rows(*pairs):
    return [
        {"context": c, "question": q, "ideal": "x", "_row": i} for i, (c, q) in enumerate(pairs, 1)
    ]


def _code_rows(n, prefix):
    return [
        {
            "context": [f"{prefix} answer {i}", "line two", "line three"],
            "code": ["x = 1"],
            "error": ["Traceback"],
            "ideal": ["x = 2"],
            "context_url": "u",
            "_row": i + 1,
        }
        for i in range(n)
    ]


def _raw():
    tables_train = [(TABLE.replace("1", str(100 + i)), f"q{i}") for i in range(10)]
    tables_train.append((tables_train[0][0], "q0-bis"))  # duplicate table, second question
    return {
        ("email", "train"): _rows(
            (EMAIL, "q1"), (EMAIL + " x", "q2"), ("shared mail. two.", "q3"), (EMAIL + " y", "q4")
        ),
        ("email", "test"): _rows(("shared mail. two.", "q5"), (EMAIL + " z", "q6")),
        ("table", "train"): _rows(*tables_train),
        ("table", "test"): _rows((TABLE, "qt")),
        ("code", "train"): _code_rows(4, "train"),
        ("code", "test"): _code_rows(2, "test"),
    }


@pytest.fixture
def p1cfg(cfg):
    bipia = cfg.bipia.model_copy(
        update={
            "tables_moved_to_test": 3,
            "validation_size": {"email": 1, "table": 2, "code": 1},
            "expected_cross_split_emails": 1,
        }
    )
    return cfg.model_copy(update={"bipia": bipia, "expected_counts": {}})


def test_dedupe_merges_questions_in_order():
    docs = p1.dedupe("table", "train", _rows((TABLE, "q1"), ("other", "q2"), (TABLE, "q3")))
    assert len(docs) == 2
    assert docs[0]["questions"] == ["q1", "q3"]
    assert docs[0]["question"] == "q1"
    assert docs[0]["n_rows"] == 2


def test_code_document_is_joined_context():
    doc = p1.dedupe("code", "train", _code_rows(1, "a"))[0]
    assert doc["document"] == "a answer 0\nline two\nline three"
    assert doc["code"] == "x = 1" and doc["question"] is None


def test_split_per_document(p1cfg):
    raw = _raw()
    res = p1.build_split(raw, p1cfg)
    # The shared email is dropped from train with its training question (q3).
    assert len(res.dropped_cross_split) == 1
    assert [q for _, q in res.disregarded_questions] == ["q3"]
    assert len(res.moved_tables) == 3
    split = {d["doc_id"]: d["split"] for d in res.docs}
    assert len(split) == len(res.docs)  # each document once
    assert p1.check_docs([d | {"middle_index": 1} for d in res.docs]) == []
    failures, comp = p1.check_test_composition(
        res.docs, raw, p1cfg.bipia.scenarios, res.moved_tables, 3
    )
    assert failures == [] and all(c["ok"] for c in comp)
    c = p1.doc_counts(res, ("scenario", "split", "source_split"))
    assert c["by_scenario"]["email"]["split"] == {"test": 2, "validation": 1, "train": 2}
    assert c["by_scenario"]["table"]["split"] == {"test": 4, "validation": 2, "train": 5}
    assert c["train_questions_disregarded"] == 1 and c["tables_moved_to_test"] == 3
    moved = [d for d in res.docs if d["doc_id"] in res.moved_tables]
    assert all(d["split"] == "test" and d["source_split"] == "train" for d in moved)
    shared = next(d for d in res.docs if d["document"] == "shared mail. two.")
    assert shared["split"] == "test" and shared["questions"] == ["q5"]
    dup = next(d for d in res.docs if d["n_rows"] == 2)
    assert dup["questions"] == ["q0", "q0-bis"]


def test_test_composition_detects_problems(p1cfg):
    raw = _raw()
    res = p1.build_split(raw, p1cfg)
    sc, moved = p1cfg.bipia.scenarios, res.moved_tables
    check = lambda docs, m=moved, n=3: p1.check_test_composition(docs, raw, sc, m, n)[0]  # noqa: E731

    assert check(res.docs) == []
    assert any("moved to test, expected 4" in f for f in check(res.docs, n=4))

    official_out = [
        d | {"split": "train"} if d["split"] == "test" and d["source_split"] == "test" else d
        for d in res.docs
    ]
    assert any("missing" in f for f in check(official_out))

    extra_in = [
        d | {"split": "test"} if d["scenario"] == "table" and d["split"] == "train" else d
        for d in res.docs
    ]
    assert any("neither official nor moved" in f for f in check(extra_in))

    edited = [
        d | {"document": d["document"] + "!"} if d["source_split"] == "test" else d
        for d in res.docs
    ]
    assert check(edited)


def test_split_is_deterministic(p1cfg):
    a = p1.build_split(_raw(), p1cfg).docs
    b = p1.build_split(_raw(), p1cfg).docs
    assert a == b


def test_middle_index_boundaries(p1cfg):
    for scenario, text in (("email", EMAIL), ("table", TABLE), ("code", "a\nb\nc")):
        doc = {"doc_id": "d", "scenario": scenario, "document": text}
        i, method = p1.pick_middle(doc, p1cfg)
        assert (i, method) == p1.pick_middle(doc, p1cfg)
        assert 0 < i < len(text)
        if scenario == "table":
            assert method == "table_row"
            assert text[i - 1] == "\n" and text[:i].count("\n") >= 3  # after header+sep+1st row
        elif scenario == "code":
            assert text[i - 1] == "\n"
        else:
            assert method == "sentence"
            assert text[i - 2 : i] == ". "


def test_middle_fallbacks():
    assert p1.middle_candidates("email", "one sentence\nsecond line")[1] in (
        "sentence",
        "line_fallback",
    )
    cands, method = p1.middle_candidates("email", "no boundary at all here")
    assert method == "word_fallback" and len(cands) == 1


def _attack_raw(train_cats, test_cats):
    return {
        ("text", "train"): {c: [f"{c} tr {i}" for i in range(5)] for c in train_cats},
        ("text", "test"): {c: [f"{c} te {i}" for i in range(5)] for c in test_cats},
    }


def test_attacks_fields_and_shared_name_allowed():
    attacks = p1.build_attacks(
        _attack_raw(["A", "Language Translation"], ["B", "Language Translation"])
    )
    a = attacks[0]
    assert a == {
        "attack_id": "text-train-a-0",
        "group": "text",
        "attack_group": "A",
        "attack_name": "A-0",
        "text": "A tr 0",
        "source_split": "train",
    }
    failures, deviations = p1.check_attacks(attacks, ["Language Translation"])
    assert failures == [] and len(deviations) == 1


def test_attacks_shared_type_or_string_fails():
    attacks = p1.build_attacks(_attack_raw(["A"], ["A"]))
    failures, _ = p1.check_attacks(attacks, [])
    assert any("attack type 'A'" in f for f in failures)
    raw = _attack_raw(["A"], ["B"])
    raw[("text", "test")]["B"][0] = "A tr 0"
    failures, _ = p1.check_attacks(p1.build_attacks(raw), [])
    assert any("attack strings" in f for f in failures)


def test_run_end_to_end(p1cfg, monkeypatch):
    root = p1cfg.paths.raw / "BIPIA" / "benchmark"
    for (scenario, split), rows in _raw().items():
        (root / scenario).mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({k: v for k, v in r.items() if k != "_row"}) for r in rows]
        (root / scenario / f"{split}.jsonl").write_text("\n".join(lines), encoding="utf-8")
    for (group, split), name in ATTACK_FILES.items():
        cats = {
            f"{group}-{split}-{c}": [f"{group} {split} {c} {i}" for i in range(5)] for c in range(2)
        }
        (root / name).write_text(json.dumps(cats), encoding="utf-8")
    monkeypatch.setattr(p1, "fetch_bipia", lambda cfg: "deadbeef")

    out = p1.run(p1cfg)
    docs = [json.loads(x) for x in out["docs"].read_text("utf-8").splitlines()]
    assert len(docs) == 5 + 11 + 6  # distinct docs: email 4+2-1, table 10+1, code 4+2
    assert len(out["attacks"].read_text("utf-8").splitlines()) == 40
    md = out["report"].read_text("utf-8")
    assert "How the split is built" in md and "FAILED" not in md and "deadbeef" in md
    assert "Test composition" in md and "of which moved from train" in md
    assert "Note for the team" not in md  # synthetic attacks share no names

    first = out["docs"].read_bytes()
    p1.run(p1cfg)
    assert out["docs"].read_bytes() == first  # byte-identical rerun


def test_run_stops_on_count_mismatch(p1cfg, monkeypatch):
    test_run_end_to_end(p1cfg, monkeypatch)
    out_docs = p1cfg.paths.interim / p1.DOCS_FILE
    out_docs.unlink()
    bad = p1cfg.model_copy(update={"expected_counts": {"p1_docs": {"total": 999}}})
    with pytest.raises(p1.P1CheckError, match="total: expected 999"):
        p1.run(bad)
    assert not out_docs.exists()
    assert "FAILED" in (p1cfg.paths.reports / "p1" / "p1_counts.md").read_text("utf-8")


def test_table_without_separator_keeps_header_and_first_row():
    text = "| h1 | h2 |\n| a | b |\n| c | d |\n| e | f |"
    cands, method = p1.middle_candidates("table", text)
    assert method == "table_row"
    assert cands == [text.index("| c"), text.index("| e")]


def test_glued_sentence_fallback():
    text = "Hi David,Your account has been charged $3,620.00.They initiated this charge today"
    cands, method = p1.middle_candidates("email", text)
    assert method == "glued_sentence_fallback"
    assert text[cands[0] :].startswith("They")


def test_shared_name_note():
    attacks = p1.build_attacks(
        _attack_raw(["A", "Language Translation"], ["B", "Language Translation"])
    )
    note = " ".join(p1._shared_name_note(attacks, ["Language Translation"]).split())
    assert '"Language Translation"' in note and "per `attack_group`" in note
    assert "0 of the 10 strings" in note and "5 of the 10 text test attacks (50%)" in note
    assert p1._shared_name_note(attacks, []) == ""
