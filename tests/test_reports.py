import json

import pytest

from dataset_builder.reports import CountMismatchError, check_expected, write_counts_report

ROWS = [
    {"scenario": "email", "split": "test", "label": 1, "position": "start"},
    {"scenario": "email", "split": "test", "label": 0, "position": "start"},
    {"scenario": "code", "split": "train", "label": 1, "position": None},
]


def test_write_counts_report(cfg):
    counts = write_counts_report("p1_demo", ROWS, cfg=cfg)
    assert counts["total"] == 3
    assert counts["scenario"] == {"code": 1, "email": 2}
    assert counts["position"] == {"n/a": 1, "start": 2}
    assert counts["cross"]["counts"]["email | test | 1 | start"] == 1
    on_disk = json.loads(
        (cfg.paths.reports / "p1" / "p1_demo_counts.json").read_text(encoding="utf-8")
    )
    assert on_disk == counts
    assert "total rows: **3**" in (cfg.paths.reports / "p1" / "p1_demo_counts.md").read_text(
        "utf-8"
    )


def test_check_expected(cfg):
    counts = write_counts_report("s", ROWS, cfg=cfg)
    check_expected("s", counts, {"total": 3, "scenario": {"email": 2}})
    check_expected("s", counts, cfg=cfg)  # no expectation defined -> no-op
    with pytest.raises(CountMismatchError, match=r"total: expected 4, got 3"):
        check_expected("s", counts, {"total": 4, "label": {"1": 2}})
    with pytest.raises(CountMismatchError, match=r"split.validation: expected 1, got 0"):
        check_expected("s", counts, {"split": {"validation": 1}})


def test_check_expected_nested():
    actual = {"by_scenario": {"email": {"split": {"test": 44, "train": 26}}}}
    check_expected("s", actual, {"by_scenario": {"email": {"split": {"test": 44}}}})
    with pytest.raises(CountMismatchError, match=r"by_scenario\.email\.split\.train: expected 3"):
        check_expected("s", actual, {"by_scenario": {"email": {"split": {"train": 3}}}})


def test_report_sections(cfg):
    write_counts_report(
        "x", ROWS, cfg=cfg, preamble=[("Intro", "hello")], sections=[("End", "bye")]
    )
    md = (cfg.paths.reports / "x_counts.md").read_text("utf-8")
    assert md.index("## Intro") < md.index("## Counts") < md.index("## End")
