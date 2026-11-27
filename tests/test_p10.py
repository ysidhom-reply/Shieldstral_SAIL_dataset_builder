import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataset_builder.reports import CountMismatchError
from dataset_builder.schema import columns_for
from dataset_builder.steps import p10_push as p10

SIZES = {("bipia", "train"): 8, ("bipia", "validation"): 4, ("bipia", "test"): 6}
SIZES[("nemotron", "test")] = 4
SIZES[("bipia_textfooler", "test")] = 6  # 2 variants, 2 plain twins, 2 perturbed twins
TRAINING = {("bipia_textfooler", "train"): 8, ("bipia_textfooler", "validation"): 4}


def _table(config: str, split: str, n: int, extra: bool = False) -> pa.Table:
    cols: dict[str, list] = {c: [None] * n for c in columns_for(config)}
    cols["sample_id"] = [f"{config}-{split}-{i}" for i in range(n)]
    cols["source"] = [config] * n
    cols["split"] = [split] * n
    tf_test = (config, split) == ("bipia_textfooler", "test")
    cols["label"] = [int(i % 3 == 0) if tf_test else i % 2 for i in range(n)]
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


def _write_p6(cfg):
    bucket = {"processed": 30, "skipped": 4, "succeeded": 12, "failed": 14, "kept": 2}
    payload = {
        "split": {"test": 6},
        "failures": [],
        "stats": {
            "by_bucket": {"email/1": bucket, "email/0": bucket},
            "all": {"succeeded": 24, "failed": 28},
        },
        "info": {
            "victim": "protectai/victim-model@abc999",
            "victim_license": "apache-2.0",
            "versions": {"textattack": "0.3.11"},
            "constraints": {
                "max_candidates": 50,
                "min_cos_sim": 0.5,
                "use_threshold": 0.84,
                "use_window": 15,
                "max_span_fraction": 0.3,
                "search": "GreedyWordSwapWIR",
                "goal": "UntargetedClassification",
            },
        },
    }
    path = cfg.paths.reports / "p6" / "p6_textfooler.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_p7(cfg, version="v0", status="PUSH ALLOWED", exit_code=0, newer=True):
    ok = {"auc": 0.6, "recall": 0.1, "n": 6}
    payload = {
        "status": status,
        "exit_code": exit_code,
        "limits": {"length_auc_stop": 0.7, "tfidf_auc_stop": 0.95},
        "baselines": {"length": {"bipia test": {"all": ok}}, "tfidf": {"bipia test": {"all": ok}}},
        "nemotron_length": {"all": {"auc": 0.98, "positive_longer": 1.0}},
    }
    path = cfg.paths.reports / "p7" / f"p7_checks_{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    t = time.time() + (100 if newer else -100)
    os.utime(path, (t, t))


def _write_data(cfg, extra=False):
    for (config, split), n in SIZES.items():
        path = cfg.paths.out / config / f"{split}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(_table(config, split, n, extra=extra and split == "test"), path)


@pytest.fixture
def ready(cfg, monkeypatch):
    _write_reports(cfg)
    _write_data(cfg)
    _write_p7(cfg)
    monkeypatch.setattr(p10, "_known_secrets", lambda: [])
    return cfg


@pytest.fixture
def ready_v1(ready):
    _write_p6(ready)
    _write_p7(ready, "v1")
    return ready


def test_dry_run_builds_card_and_never_calls_the_hub(ready, monkeypatch):
    import huggingface_hub

    def boom(*a, **k):
        raise AssertionError("the dry-run must not create an HfApi")

    monkeypatch.setattr(huggingface_hub, "HfApi", boom)
    out = p10.run(ready, version="v0")

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
        p10.run(cfg)


def test_preflight_stops_on_count_mismatch(cfg, monkeypatch):
    _write_reports(cfg, p4_train=10)
    _write_data(cfg)
    with pytest.raises(CountMismatchError, match="bipia/train: 8 rows, the report says 10"):
        p10.run(cfg)


def test_preflight_stops_on_extra_column(cfg):
    _write_reports(cfg)
    _write_data(cfg, extra=True)
    with pytest.raises(CountMismatchError, match=r"extra \['doc_id'\]"):
        p10.run(cfg)


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
        p10.run(ready)


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

    def upload_file(self, **kw):
        self.calls.append(("upload_file", kw))
        return SimpleNamespace(oid="cafe01", commit_url="https://example/card")


def test_push_uploads_and_tags(ready):
    api = FakeApi()
    out = p10.run(ready, version="v0", do_push=True, api=api)
    assert out["push"] == {
        "commit": "c0ffee",
        "commit_url": "https://example/commit",
        "tag": "v0",
    }
    (kind, up), (kind2, tag) = api.calls
    assert (
        kind == "upload" and up["repo_type"] == "dataset" and up["repo_id"] == ready.dataset.repo_id
    )
    assert kind2 == "tag" and tag["tag"] == "v0" and tag["revision"] == "c0ffee"


def test_push_refuses_public_repo(ready):
    api = FakeApi(private=False)
    with pytest.raises(RuntimeError, match="not private"):
        p10.run(ready, do_push=True, api=api)
    assert api.calls == []


def test_push_refuses_existing_tag(ready):
    api = FakeApi(tags=["v0"])
    with pytest.raises(RuntimeError, match="already exists"):
        p10.run(ready, version="v0", do_push=True, api=api)
    assert api.calls == []


# ------------------------------------------------------------------ P7 gate


def test_gate_open_when_p7_allows_and_is_newer_than_the_data(ready):
    gate = p10.p7_gate(ready, "v0", ["bipia", "nemotron"])
    assert gate["ok"] and gate["status"] == "PUSH ALLOWED"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"status": "PUSH BLOCKED", "exit_code": 2}, "PUSH BLOCKED"),
        ({"newer": False}, "older than the data"),
    ],
)
def test_gate_closed_when_p7_blocks_or_is_stale(ready, kwargs, reason):
    _write_p7(ready, **kwargs)
    gate = p10.p7_gate(ready, "v0", ["bipia", "nemotron"])
    assert not gate["ok"] and reason in gate["reason"]


def test_gate_closed_when_p7_is_missing(cfg):
    assert "not found" in p10.p7_gate(cfg, "v1", ["bipia"])["reason"]


def test_push_is_blocked_by_the_gate_but_a_dry_run_still_works(ready):
    _write_p7(ready, status="PUSH BLOCKED", exit_code=2)
    out = p10.run(ready)  # dry-run: reports the closed gate, uploads nothing
    assert out["gate"]["ok"] is False and "A push is blocked" in out["report"].read_text("utf-8")
    api = FakeApi()
    with pytest.raises(p10.PushBlockedError, match="PUSH BLOCKED"):
        p10.run(ready, do_push=True, api=api)
    assert api.calls == []


# ------------------------------------------------------------------ v1


def test_v1_stages_only_the_new_config_and_a_card_with_the_textfooler_section(ready_v1):
    out = p10.run(ready_v1, version="v1")
    staged = sorted(
        p.relative_to(out["stage_dir"]).as_posix()
        for p in out["card"].parent.rglob("*")
        if p.is_file()
    )
    assert staged == ["README.md", "bipia_textfooler/test.parquet"]  # v0 files not uploaded again
    assert out["uploads"] == ["bipia_textfooler/test"] and out["gate"]["ok"]
    card = out["card"].read_text(encoding="utf-8")
    assert "config_name: bipia_textfooler" in card and "config_name: bipia" in card
    assert "How `bipia_textfooler` was built" in card and "Not yet included" not in card
    assert "protectai/victim-model" in card and "revision `abc999`" in card
    assert "apache-2.0" in card  # the victim's licence, from its model card
    assert "TextAttack" in card and "MIT" in card
    assert "Only variants that fooled the victim are kept" in card
    assert "0.84" in card and "no windows" in card  # constraint values and the prompt rule
    assert "| email | attacked (1) | 30 | 4 | 12 | 14 | 2 |" in card  # attempts per class
    assert "Version `v1`" in card


def test_v1_needs_the_p6_report(ready):
    with pytest.raises(FileNotFoundError, match="p6_textfooler"):
        p10.run(ready, version="v1")


def test_v1_push_needs_the_v0_tag_and_never_uploads_v0_files(ready_v1):
    api = FakeApi(tags=[])
    with pytest.raises(RuntimeError, match="tag 'v0' does not exist"):
        p10.run(ready_v1, version="v1", do_push=True, api=api)
    api = FakeApi(tags=["v0"])
    out = p10.run(ready_v1, version="v1", do_push=True, api=api)
    assert out["push"]["tag"] == "v1"
    (_, up), (_, tag) = api.calls
    uploaded = sorted(p.name for p in Path(up["folder_path"]).rglob("*"))
    assert "train.parquet" not in uploaded and "test.parquet" in uploaded
    assert tag["tag"] == "v1"


def test_v1_refuses_an_existing_tag(ready_v1):
    api = FakeApi(tags=["v0", "v1"])
    with pytest.raises(RuntimeError, match="already exists"):
        p10.run(ready_v1, version="v1", do_push=True, api=api)


def test_unknown_version(ready):
    with pytest.raises(ValueError, match="unknown version"):
        p10.run(ready, version="v9")


def test_compare_files_finds_changed_and_missing_files(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        (d / "x").mkdir(parents=True)
        (d / "x" / "one.parquet").write_bytes(b"same")
        (d / "x" / "two.parquet").write_bytes(b"same")
    assert p10.compare_files(a, b, ["x/one.parquet", "x/two.parquet"]) == []
    (b / "x" / "two.parquet").write_bytes(b"changed")
    (b / "x" / "one.parquet").unlink()
    diff = p10.compare_files(a, b, ["x/one.parquet", "x/two.parquet"])
    assert diff == ["x/one.parquet: missing in second", "x/two.parquet: content differs"]


def test_card_is_short_and_has_sources_splits_and_how_it_was_built(ready):
    card = p10.run(ready)["card"].read_text(encoding="utf-8")
    assert len(card.splitlines()) < 160
    for heading in (
        "## Sources",
        "## Splits",
        "### `bipia`",
        "## How `bipia` was built",
        "## Columns",
        "## Licences and credit",
    ):
        assert heading in card
    assert "Split by document" in card and "sampled" in card  # the split choices
    assert "| split | documents | attacks per document | rows | email | table | code |" in card
    assert "Mistral Small" in card and "Mistral Medium" in card  # synthetic benign texts
    for gone in ("<details>", "Prompt A", "rejection rate", "recall at 1% FPR"):
        assert gone not in card  # prompts, filter tables and baselines stay out of the card
    assert "## Known limits" not in card


def test_card_only_uploads_just_the_readme_and_never_tags(ready):
    api = FakeApi(tags=["v0"])
    out = p10.run(ready, version="v0", do_push=True, card_only=True, api=api)
    assert out["uploads"] == [] and out["card_push"]["tag_moved"] is False
    ((kind, up),) = api.calls  # one upload_file call: no upload_folder, no create_tag
    assert kind == "upload_file" and up["path_in_repo"] == "README.md"
    staged = sorted(
        p.relative_to(out["stage_dir"]).as_posix()
        for p in out["card"].parent.rglob("*")
        if p.is_file()
    )
    assert staged == ["README.md"]  # no parquet file is even staged
    assert "Only `README.md` was uploaded" in out["report"].read_text("utf-8")


def test_card_only_needs_the_tag_a_private_repo_and_the_gate(ready):
    with pytest.raises(RuntimeError, match="does not exist"):
        p10.run(ready, do_push=True, card_only=True, api=FakeApi(tags=[]))
    with pytest.raises(RuntimeError, match="not private"):
        p10.run(ready, do_push=True, card_only=True, api=FakeApi(private=False, tags=["v0"]))
    _write_p7(ready, status="PUSH BLOCKED", exit_code=2)
    with pytest.raises(p10.PushBlockedError):
        p10.run(ready, do_push=True, card_only=True, api=FakeApi(tags=["v0"]))


@pytest.fixture
def ready_v2(ready_v1):
    for (config, split), n in TRAINING.items():
        pq.write_table(_table(config, split, n), ready_v1.paths.out / config / f"{split}.parquet")
    payload = {
        "split": {"train": 8, "validation": 4},
        "failures": [],
        "overall": {"kept": 12, "pass_rate": 0.8},
        "assembly": {
            "train": {"per_class": 4, "nested": {"500": None, "1000": None}},
            "validation": {"per_class": 2, "nested": {}},
        },
        "info": {"generator": "Qwen/Qwen3-14B", "generator_revision": "abc", "judge": "gpt-oss"},
    }
    path = ready_v1.paths.reports / "p8" / "p8_training_variants.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    _write_p7(ready_v1, "v2")
    return ready_v1


def test_v2_stages_only_the_training_files_with_a_card_section(ready_v2):
    out = p10.run(ready_v2, version="v2")
    staged = sorted(
        p.relative_to(out["stage_dir"]).as_posix()
        for p in out["card"].parent.rglob("*")
        if p.is_file()
    )
    assert staged == [
        "README.md",
        "bipia_textfooler/train.parquet",
        "bipia_textfooler/validation.parquet",
    ]  # v0 and v1 files are not uploaded again
    card = out["card"].read_text(encoding="utf-8")
    assert "path: bipia_textfooler/train.parquet" in card
    assert "path: bipia_textfooler/test.parquet" in card
    assert "training variants were built (v2)" in card and "Qwen/Qwen3-14B" in card
    assert out["files"]["bipia_textfooler/train"]["rows"] == 8


def test_v2_needs_the_p8_report(ready_v1):
    _write_p7(ready_v1, "v2")
    with pytest.raises(FileNotFoundError, match="p8_training_variants"):
        p10.run(ready_v1, version="v2")
