import pytest

from dataset_builder.io import OutputExistsError, pilot, prepare_out, read_jsonl, rng, write_jsonl


def test_prepare_out_requires_force(cfg):
    target = cfg.paths.out / "bipia" / "train.jsonl"
    prepare_out(target, cfg=cfg)
    write_jsonl(target, [{"a": 1}])
    with pytest.raises(OutputExistsError):
        prepare_out(target, cfg=cfg)
    prepare_out(target, force=True, cfg=cfg)
    assert not target.exists()


def test_interim_can_be_overwritten(cfg):
    target = cfg.paths.interim / "x.jsonl"
    write_jsonl(target, [{"a": 1}])
    prepare_out(target, cfg=cfg)  # no error
    write_jsonl(target, [{"a": 2}, {"a": 3}])
    assert list(read_jsonl(target)) == [{"a": 2}, {"a": 3}]


def test_rng_is_deterministic_and_per_step(cfg):
    assert rng("a", cfg).random() == rng("a", cfg).random()
    assert rng("a", cfg).random() != rng("b", cfg).random()


def test_pilot_is_deterministic(cfg):
    items = list(range(500))
    p = pilot(items, "step", cfg=cfg)
    assert p == pilot(items, "step", cfg=cfg)
    assert len(p) == cfg.pilot_size == 20
    assert p == sorted(p)
    assert pilot(items[:5], "step", cfg=cfg) == items[:5]
