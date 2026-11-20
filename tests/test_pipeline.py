import json

import pytest

from dataset_builder.pipeline import build, split, transform, validate
from dataset_builder.schema import Record


def test_validate_drops_invalid_rows():
    rows = [{"id": "1", "text": "ok"}, {"id": "2", "text": ""}, {"text": "no id"}]
    assert [r.id for r in validate(rows)] == ["1"]


def test_transform_normalizes_and_dedupes():
    recs = [Record(id="1", text="a  b"), Record(id="2", text="a b")]
    out = list(transform(recs))
    assert len(out) == 1
    assert out[0].text == "a b"


def test_split_is_deterministic_and_complete():
    recs = [Record(id=str(i), text=f"t{i}") for i in range(100)]
    a, b = split(recs, seed=1), split(recs, seed=1)
    assert a == b
    assert sum(len(v) for v in a.values()) == 100


def test_split_rejects_bad_ratios():
    with pytest.raises(ValueError):
        split([], ratios=(0.5, 0.5, 0.5))


def test_build_end_to_end(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    lines = [json.dumps({"id": str(i), "text": f"sample {i}"}) for i in range(10)]
    (raw / "input.jsonl").write_text("\n".join(lines), encoding="utf-8")

    paths = build(raw, tmp_path / "out", seed=0)

    assert set(paths) == {"train", "validation", "test"}
    total = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in paths.values())
    assert total == 10
