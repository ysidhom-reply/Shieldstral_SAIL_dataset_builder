"""File I/O helpers, seeded randomness and the pilot slice."""

import json
import os
import random
import shutil
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dataset_builder.config import Config, load_config


class OutputExistsError(FileExistsError):
    """Raised when a step would overwrite data/out without --force."""


def prepare_out(path: Path, force: bool = False, cfg: Config | None = None) -> Path:
    """Make `path` ready to be written. Refuses to overwrite anything under data/out unless forced.

    Interim paths may be overwritten freely.
    """
    cfg = cfg or load_config()
    path = Path(path).resolve()
    under_out = path.is_relative_to(cfg.paths.out.resolve())
    exists = path.is_file() or (path.is_dir() and any(path.iterdir()))
    if under_out and exists:
        if not force:
            raise OutputExistsError(f"{path} already exists. Re-run with --force to overwrite.")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any] | BaseModel]) -> int:
    """Write rows atomically (temp file, then rename). Returns the number of rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            data = row.model_dump() if isinstance(row, BaseModel) else row
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def rng(step: str, cfg: Config | None = None) -> random.Random:
    """A deterministic random stream per step, derived from the global seed."""
    cfg = cfg or load_config()
    return random.Random(f"{cfg.seed}:{step}")


def pilot[T](
    items: Sequence[T], step: str, n: int | None = None, cfg: Config | None = None
) -> list[T]:
    """A deterministic sample of `n` items (default: pilot_size), kept in original order."""
    cfg = cfg or load_config()
    n = cfg.pilot_size if n is None else n
    if n >= len(items):
        return list(items)
    idx = sorted(rng(f"pilot:{step}", cfg).sample(range(len(items)), n))
    return [items[i] for i in idx]


def arrow_schema(model: type[BaseModel]) -> Any:
    """The pyarrow schema of a model (column order = model fields): the shared contract types."""
    import types
    import typing

    import pyarrow as pa

    def arrow_type(ann: Any) -> pa.DataType:
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        origin = typing.get_origin(ann)
        if origin in (typing.Union, types.UnionType):
            return arrow_type(args[0])
        if origin is list:
            return pa.list_(arrow_type(args[0]))
        if origin is typing.Literal:
            return arrow_type(type(args[0]))
        return {str: pa.string(), int: pa.int64(), bool: pa.bool_(), float: pa.float64()}[ann]

    return pa.schema([(n, arrow_type(f.annotation)) for n, f in model.model_fields.items()])


def write_parquet(
    path: Path,
    rows: Sequence[BaseModel],
    extra_columns: dict[str, Sequence[str]] | None = None,
) -> int:
    """Write Samples as parquet with an explicit schema (column order = model fields).

    `extra_columns` (optional) adds interim-only string columns after the model fields, e.g.
    `doc_id`; each must have one value per row. Writes to a temp file, then renames. Returns the
    number of rows.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not rows:
        raise ValueError("refusing to write an empty parquet file")
    schema = arrow_schema(type(rows[0]))
    table = pa.Table.from_pylist([r.model_dump() for r in rows], schema=schema)
    for name, values in (extra_columns or {}).items():
        if len(values) != len(rows):
            raise ValueError(f"extra column {name!r} has {len(values)} values for {len(rows)} rows")
        table = table.append_column(pa.field(name, pa.string()), pa.array(values, pa.string()))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp)
    os.replace(tmp, path)
    return table.num_rows


def read_parquet(path: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq

    return pq.read_table(path).to_pylist()
