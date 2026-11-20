"""Dataset pipeline: load -> validate -> transform -> split -> export."""

import json
import logging
import random
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import ValidationError

from dataset_builder.schema import Record

logger = logging.getLogger(__name__)


def load_raw(raw_dir: Path) -> Iterator[dict]:
    """Yield raw rows from every `*.jsonl` file in `raw_dir`."""
    for path in sorted(raw_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping invalid JSON at %s:%d", path, line_no)


def validate(rows: Iterable[dict]) -> Iterator[Record]:
    """Turn raw rows into `Record`s, dropping the invalid ones."""
    for row in rows:
        try:
            yield Record.model_validate(row)
        except ValidationError as e:
            logger.warning("Dropping invalid row %r: %s", row.get("id"), e.errors()[0]["msg"])


def transform(records: Iterable[Record]) -> Iterator[Record]:
    """Clean / enrich records. Add your own processing steps here."""
    seen: set[str] = set()
    for r in records:
        text = " ".join(r.text.split())
        if text in seen:  # exact-duplicate removal
            continue
        seen.add(text)
        yield r.model_copy(update={"text": text})


def split(
    records: list[Record], ratios: tuple[float, float, float] = (0.8, 0.1, 0.1), seed: int = 42
) -> dict[str, list[Record]]:
    """Shuffle deterministically and split into train / validation / test."""
    if abs(sum(ratios) - 1.0) > 1e-9:
        raise ValueError(f"Split ratios must sum to 1, got {ratios}")
    shuffled = records[:]
    random.Random(seed).shuffle(shuffled)
    n = len(shuffled)
    n_train = int(n * ratios[0])
    n_val = int(n * ratios[1])
    return {
        "train": shuffled[:n_train],
        "validation": shuffled[n_train : n_train + n_val],
        "test": shuffled[n_train + n_val :],
    }


def export_jsonl(splits: dict[str, list[Record]], out_dir: Path) -> dict[str, Path]:
    """Write each split to `<out_dir>/<split>.jsonl`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name, records in splits.items():
        path = out_dir / f"{name}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for r in records:
                f.write(r.model_dump_json() + "\n")
        paths[name] = path
    return paths


def build(raw_dir: Path, out_dir: Path, seed: int = 42) -> dict[str, Path]:
    records = list(transform(validate(load_raw(raw_dir))))
    logger.info("Loaded %d valid records from %s", len(records), raw_dir)
    paths = export_jsonl(split(records, seed=seed), out_dir)
    for name, path in paths.items():
        logger.info("Wrote %s -> %s", name, path)
    return paths
