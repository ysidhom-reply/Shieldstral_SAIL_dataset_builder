import json
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataset_builder.reports import CountMismatchError
from dataset_builder.schema import COLUMNS
from dataset_builder.steps import p10_push as p10

SIZES = {("bipia", "train"): 8, ("bipia", "validation"): 4, ("bipia", "test"): 6}
SIZES[("nemotron", "test")] = 4


def _table(config: str, split: str, n: int, extra: bool = False) -> pa.Table:
    cols: dict[str, list] = {c: [None] * n for c in COLUMNS}
    cols["sample_id"] = [f"{config}-{split}-{i}" for i in range(n)]
    cols["source"] = [config] * n
    cols["split"] = [split] * n
    cols["label"] = [i % 2 for i in range(n)]
    cols["document"] = ["some document"] * n
    cols["protected_tokens"] = [[] for _ in range(n)]
    if extra:
        cols["doc_id"] = ["d"] * n
    types = {"label": pa.int64(), "protected_tokens": pa.list_(pa.string())}
    return pa.table({k: pa.array(v, type=types.get(k, pa.string())) for k, v in cols.items()})


def _write_reports(cfg, failures=None, p4_train=8):
    r = cfg.paths.reports
    by_split = {
        s: {"total": n, "scenario": {"email": n // 2, "table": n // 2, "code": 0}}
        for (c, s), n in SIZES.items()
        if c == "bipia"
    }
    data = {
        "p1/p1_counts.json": {
            "repo_commit": "abc123",
            "p1_docs": {
                "total": 10,
                "scenario": {"email": 5, "table": 5},
                "split": {"train": 6, "validation": 2, "test": 2},
                "tables_moved_to_test": 1,
                "cross_split_emails_dropped": 0,
            },
            "p1_attacks": {"total": 250},
            "failures": [],
        },
        "p2/p2_counts.json": {"failures": failures or []},
        "p3/p3_rejections.json": {
            "total": 100,
            "generated": 130,
            "reach": {"email|start|1": 130, "email|start|2": 120, "email|start|3": 110},
            "rejected": {"email|start|1": 10, "email|start|2": 10, "email|start|3": 10},
            "judge_control": {"n": 40, "flagged": 40},
            "generator": {"requested": "gen", "id": "gen-1"},
            "judge": {"requested": "judge", "id": "judge-1"},
            "failures": [],
        },
        "p4/p4_composition.json": {
            "split": {"train": p4_train, "validation": 4, "test": 6},
            "p4_bipia": {"by_split": by_split},
            "pairs": 9,
            "length_matched": 8,
            "failures": [],
        },
        "p5/p5_nemotron.json": {
            "split": {"test": 4},
            "total": 4,
            "stats": {"records": 2, "sentence_repaired": 1},
            "revision": "def456",
            "failures": [],
        },
    }
    for rel, payload in data.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text(json.dumps(payload), encoding="utf-8")


def _write_data(cfg, extra=False):
    for (config, split), n in SIZES.items():
        path = cfg.paths.out / config / f"{split}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(_table(config, split, n, extra=extra and split == "test"), path)


@pytest.fixture
def ready(cfg, monkeypatch):
    _write_reports(cfg)
    _write_data(cfg)
    monkeypatch.setattr(p10, "_known_secrets", lambda: [])
    return cfg


def test_dry_run_builds_card_and_never_calls_the_hub(ready, monkeypatch):
    import huggingface_hub

    def boom(*a, **k):
        raise AssertionError("the dry-run must not create an HfApi")

    monkeypatch.setattr(huggingface_hub, "HfApi", boom)
    out = p10.run(ready, tag="v0.1", configs=["bipia", "nemotron"])

    assert out["action"] == "dry-run" and "push" not in out
    card = out["card"].read_text(encoding="utf-8")
    assert "license: cc-by-sa-4.0" in card
    assert "config_name: bipia" in card and "config_name: nemotron" in card
    assert "path: bipia/validation.parquet" in card
    assert "Not yet included: `bipia_textfooler`" in card
    assert "CC BY 4.0" in card and "NVIDIA" in card
    assert "commit `abc123`" in card and "revision `def456`" in card
    staged = sorted(
        p.relative_to(out["stage_dir"]).as_posix()
        for p in out["card"].parent.rglob("*")
        if p.is_file()
    )
    assert staged == [
        "README.md",
        "bipia/test.parquet",
        "bipia/train.parquet",
        "bipia/validation.parquet",
        "nemotron/test.parquet",
    ]
    assert out["report"].is_file()


def test_preflight_stops_on_failed_step_report(cfg, monkeypatch):
    _write_reports(cfg, failures=["something failed"])
    _write_data(cfg)
    monkeypatch.setattr(p10, "_known_secrets", lambda: [])
    with pytest.raises(RuntimeError, match="failed checks"):
        p10.run(cfg, configs=["bipia", "nemotron"])


def test_preflight_stops_on_count_mismatch(cfg, monkeypatch):
    _write_reports(cfg, p4_train=10)
    _write_data(cfg)
    with pytest.raises(CountMismatchError, match="bipia/train: 8 rows, the report says 10"):
        p10.run(cfg, configs=["bipia", "nemotron"])


def test_preflight_stops_on_extra_column(cfg):
    _write_reports(cfg)
    _write_data(cfg, extra=True)
    with pytest.raises(CountMismatchError, match=r"extra \['doc_id'\]"):
        p10.run(cfg, configs=["bipia", "nemotron"])


def test_scan_secrets_finds_patterns_and_known_values(tmp_path):
    clean = tmp_path / "clean.md"
    clean.write_text("nothing here, HF_TOKEN is read from the environment", encoding="utf-8")
    pattern = tmp_path / "pattern.md"
    pattern.write_text("token: hf_" + "a" * 34, encoding="utf-8")
    value = tmp_path / "value.bin"
    value.write_bytes(b"xx my-secret-value-123 yy")
    hits = p10.scan_secrets([clean, pattern, value], [b"my-secret-value-123"])
    assert str(clean) not in hits
    assert hits[str(pattern)] == ["hf_token"]
    assert hits[str(value)] == ["known_secret_value"]


def test_secret_in_staged_file_stops_the_run(ready, monkeypatch):
    monkeypatch.setattr(p10, "_known_secrets", lambda: [b"abc123"])  # the BIPIA commit in the card
    with pytest.raises(p10.SecretFoundError):
        p10.run(ready, configs=["bipia", "nemotron"])


def test_filter_rates_from_report():
    p3 = {
        "reach": {"a|start|1": 10, "b|end|1": 5, "a|start|2": 12},
        "rejected": {"a|start|1": 2, "b|end|1": 1, "a|start|2": 3},
    }
    rows = {r["name"]: (r["checked"], r["rejected"]) for r in p10.filter_rates(p3)}
    assert rows["shape"] == (15, 3)
    assert rows["forbidden content"] == (12, 3)
    assert rows["judge"] == (0, 0)


class FakeApi:
    def __init__(self, private=True, tags=()):
        self.private, self.tags, self.calls = private, list(tags), []

    def repo_info(self, repo, repo_type):
        return SimpleNamespace(private=self.private)

    def list_repo_refs(self, repo, repo_type):
        return SimpleNamespace(tags=[SimpleNamespace(name=t) for t in self.tags])

    def upload_folder(self, **kw):
        self.calls.append(("upload", kw))
        return SimpleNamespace(oid="c0ffee", commit_url="https://example/commit")

    def create_tag(self, repo, **kw):
        self.calls.append(("tag", kw))


def test_push_uploads_and_tags(ready):
    api = FakeApi()
    out = p10.run(ready, do_push=True, tag="v0.1", configs=["bipia", "nemotron"], api=api)
    assert out["push"] == {
        "commit": "c0ffee",
        "commit_url": "https://example/commit",
        "tag": "v0.1",
    }
    (kind, up), (kind2, tag) = api.calls
    assert (
        kind == "upload" and up["repo_type"] == "dataset" and up["repo_id"] == ready.dataset.repo_id
    )
    assert kind2 == "tag" and tag["tag"] == "v0.1" and tag["revision"] == "c0ffee"


def test_push_refuses_public_repo(ready):
    api = FakeApi(private=False)
    with pytest.raises(RuntimeError, match="not private"):
        p10.run(ready, do_push=True, configs=["bipia", "nemotron"], api=api)
    assert api.calls == []


def test_push_refuses_existing_tag(ready):
    api = FakeApi(tags=["v0.1"])
    with pytest.raises(RuntimeError, match="already exists"):
        p10.run(ready, do_push=True, tag="v0.1", configs=["bipia", "nemotron"], api=api)
    assert api.calls == []
