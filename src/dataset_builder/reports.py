"""Counts reports written to reports/ after every step, and checks against expected counts."""

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dataset_builder.config import Config, load_config

DEFAULT_KEYS = ("scenario", "split", "label", "position")
NA = "n/a"


class CountMismatchError(RuntimeError):
    """Raised when counts differ from the expected values in the brief."""


def _get(row: Mapping[str, Any] | BaseModel, key: str) -> Any:
    return getattr(row, key, None) if isinstance(row, BaseModel) else row.get(key)


def compute_counts(
    rows: Iterable[Mapping[str, Any] | BaseModel], keys: Iterable[str] = DEFAULT_KEYS
) -> dict[str, Any]:
    rows = list(rows)
    keys = list(keys)
    counts: dict[str, Any] = {"total": len(rows)}
    for key in keys:
        c = Counter(NA if (v := _get(r, key)) is None else str(v) for r in rows)
        counts[key] = dict(sorted(c.items()))
    # Cross-tab of all keys, so per-scenario / split / class / position rows are visible together.
    cross = Counter(
        " | ".join(NA if (v := _get(r, k)) is None else str(v) for k in keys) for r in rows
    )
    counts["cross"] = {"keys": keys, "counts": dict(sorted(cross.items()))}
    return counts


Section = tuple[str, str]  # (title, markdown body)


def _sections_md(sections: Iterable[Section]) -> list[str]:
    lines: list[str] = []
    for title, body in sections:
        lines += [f"## {title}", "", body.rstrip(), ""]
    return lines


def _to_markdown(
    step: str,
    counts: dict[str, Any],
    preamble: Iterable[Section] = (),
    sections: Iterable[Section] = (),
) -> str:
    lines = [f"# Report: {step}", ""]
    lines += _sections_md(preamble)
    lines += [f"## Counts (total rows: **{counts['total']}**)", ""]
    for key, values in counts.items():
        if key in ("total", "cross"):
            continue
        lines += [f"### {key}", "", f"| {key} | rows |", "|---|---:|"]
        lines += [f"| {k} | {v} |" for k, v in values.items()]
        lines.append("")
    keys = counts["cross"]["keys"]
    lines += ["### cross", "", "| " + " | ".join(keys) + " | rows |"]
    lines.append("|" + "---|" * len(keys) + "---:|")
    lines += [f"| {k} | {v} |" for k, v in counts["cross"]["counts"].items()]
    lines.append("")
    lines += _sections_md(sections)
    return "\n".join(lines).rstrip() + "\n"


def write_counts_report(
    step: str,
    rows: Iterable[Mapping[str, Any] | BaseModel],
    keys: Iterable[str] = DEFAULT_KEYS,
    cfg: Config | None = None,
    preamble: Iterable[Section] = (),
    sections: Iterable[Section] = (),
    extra: Mapping[str, Any] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Write reports/<name>.{json,md} (default name: <step>_counts) and return the counts.

    `preamble` sections go before the counts, `sections` after. `extra` is merged into the JSON
    (e.g. further count tables a step wants to keep).
    """
    cfg = cfg or load_config()
    counts = compute_counts(rows, keys)
    out: Path = cfg.paths.reports
    out.mkdir(parents=True, exist_ok=True)
    payload = counts | dict(extra or {})
    name = name or f"{step}_counts"
    (out / f"{name}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out / f"{name}.md").write_text(
        _to_markdown(step, counts, preamble, sections), encoding="utf-8"
    )
    return counts


def _diff(path: str, exp: Any, act: Any, out: list[str]) -> None:
    if isinstance(exp, Mapping):
        act = act if isinstance(act, Mapping) else {}
        for k, v in exp.items():
            _diff(f"{path}.{k}" if path else str(k), v, act.get(str(k), act.get(k, 0)), out)
    elif act != exp:
        out.append(f"{path}: expected {exp}, got {act}")


def check_expected(
    step: str,
    actual: dict[str, Any],
    expected: dict[str, Any] | None = None,
    cfg: Config | None = None,
) -> None:
    """Compare counts with the brief. Raises on any difference; never adjusts anything.

    `expected` defaults to `expected_counts[step]` in the config. Only the keys present in the
    expected values are checked (recursively, so nested tables such as
    `by_scenario.email.split.test` work). Does nothing if no expectation is defined.
    """
    if expected is None:
        expected = (cfg or load_config()).expected_counts.get(step)
    if not expected:
        return
    diffs: list[str] = []
    _diff("", expected, actual, diffs)
    if diffs:
        raise CountMismatchError(
            f"Step {step!r}: counts differ from the brief:\n  " + "\n  ".join(diffs)
        )
