"""Build the user request and the query for a BIPIA document."""

import re
from typing import Any

from dataset_builder.config import Config
from dataset_builder.schema import build_query

_Q_PREFIX = re.compile(r"^\s*Q:\s*")
_EXCEPTION_LINE = re.compile(r"^\s*[\w.]+(Error|Exception)\b.*:")


class QueryError(ValueError):
    pass


def error_line(doc: dict[str, Any]) -> str:
    """Last non-empty line of the traceback (the exception). Falls back to the document."""
    lines = [ln.strip() for ln in (doc.get("error") or "").split("\n") if ln.strip()]
    if lines:
        return lines[-1]
    found = [ln.strip() for ln in doc["document"].split("\n") if _EXCEPTION_LINE.match(ln)]
    if found:
        return found[-1]
    raise QueryError(f"{doc['doc_id']}: no error message found")


def user_request(doc: dict[str, Any], question: str | None = None) -> str:
    """email/table: the question without its "Q:" prefix (default: the document's first
    question). code: fix-the-bug request with the traceback's last line."""
    if doc["scenario"] == "code":
        return f"Fix the bug in my code. Error: {error_line(doc)}"
    question = question if question is not None else doc.get("question")
    if not question:
        raise QueryError(f"{doc['doc_id']}: no question")
    return _Q_PREFIX.sub("", question).strip()


def query(doc: dict[str, Any], cfg: Config | None = None, question: str | None = None) -> str:
    return build_query(user_request(doc, question), cfg)


def candidate_questions(doc: dict[str, Any]) -> list[str | None]:
    """The questions a sample may use: the document's list (P1), or [None] for code."""
    if doc["scenario"] == "code":
        return [None]
    return list(doc.get("questions") or [doc.get("question")])
