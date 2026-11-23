import logging

from dataset_builder.progress import Progress


def test_progress_eta_ignores_cached_calls(caplog):
    t = {"now": 0.0}
    p = Progress("round 1 judge", total=100, every=30, clock=lambda: t["now"])
    for _ in range(20):  # 20 cached calls, instant
        p.step(cached=True)
    for _ in range(40):  # 40 API calls over 10 s -> 4 calls/s
        t["now"] += 0.25
        p.step()
    line = p.line()
    assert "60 / 100 calls (60%)" in line and "4.0 calls/s" in line and "cached 20" in line
    assert "ETA 10 s" in line  # 40 left / 4 per s


def test_progress_logs_periodically(caplog):
    t = {"now": 0.0}
    with caplog.at_level(logging.INFO, logger="dataset_builder.progress"):
        p = Progress("gen", total=10, every=30, clock=lambda: t["now"])
        for _ in range(9):
            t["now"] += 10
            p.step()
        p.finish()
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs[0] == "gen: 10 calls to make"
    assert sum("ETA" in m for m in msgs) >= 2 and msgs[-1].startswith("gen: done, 9 calls")
