"""Thread-safe progress reporting for long API stages: done / total, speed, cached, ETA."""

import logging
import threading
import time

logger = logging.getLogger("dataset_builder.progress")


def _fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


class Progress:
    """Call `step(cached=...)` once per finished call; a line is logged every `every` seconds
    and once at the end. The speed and ETA count only calls that went to the API, because
    cached calls finish instantly and would make the estimate far too optimistic."""

    def __init__(self, label: str, total: int, every: float = 30.0, clock=time.monotonic):
        self.label, self.total, self.every, self._clock = label, total, every, clock
        self.done = self.cached = 0
        self._start = self._last = clock()
        self._lock = threading.Lock()
        if total:
            logger.info("%s: %d calls to make", label, total)

    def step(self, cached: bool = False) -> None:
        with self._lock:
            self.done += 1
            self.cached += int(cached)
            now = self._clock()
            if now - self._last >= self.every and self.done < self.total:
                self._last = now
                logger.info(self.line(now))

    def line(self, now: float | None = None) -> str:
        now = self._clock() if now is None else now
        elapsed = max(now - self._start, 1e-9)
        api = self.done - self.cached
        rate = api / elapsed
        pct = self.done / self.total if self.total else 1.0
        remaining = self.total - self.done
        eta = (
            _fmt_eta(remaining / rate)
            if rate > 0 and remaining
            else ("0 s" if not remaining else "?")
        )
        return (
            f"{self.label}: {self.done:,} / {self.total:,} calls ({pct:.0%}), "
            f"{rate:.1f} calls/s, cached {self.cached:,}, ETA {eta}"
        )

    def finish(self) -> None:
        if self.total:
            elapsed = self._clock() - self._start
            api = self.done - self.cached
            logger.info(
                "%s: done, %d calls (%d from cache) in %s, %.1f calls/s",
                self.label,
                self.done,
                self.cached,
                _fmt_eta(elapsed),
                api / elapsed if elapsed > 0 else 0.0,
            )
