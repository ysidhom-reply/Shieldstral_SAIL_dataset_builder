"""Insert a text into a document at start, middle or end, and remove it again exactly.

Used for the positives (P2, attack text) and the negatives (P4, benign text), so both classes
are built the same way. Joining follows BIPIA: pieces are joined with a single newline.
"""

from typing import Literal

Position = Literal["start", "middle", "end"]
POSITIONS: tuple[Position, ...] = ("start", "middle", "end")
SEP = "\n"


class InsertionError(ValueError):
    pass


def insert(text: str, insertion: str, position: Position, middle_index: int) -> tuple[str, int]:
    """Return (new_text, insert_index). `insert_index` is where `insertion` starts in new_text.

    - start:  insertion + "\\n" + text
    - end:    text + "\\n" + insertion
    - middle: text[:m] + "\\n" + insertion + "\\n" + text[m:]   (m = middle_index)
    """
    if position == "start":
        return insertion + SEP + text, 0
    if position == "end":
        return text + SEP + insertion, len(text) + len(SEP)
    if position == "middle":
        if not 0 < middle_index < len(text):
            raise InsertionError(f"middle_index {middle_index} outside (0, {len(text)})")
        m = middle_index
        return text[:m] + SEP + insertion + SEP + text[m:], m + len(SEP)
    raise InsertionError(f"unknown position {position!r}")


def remove(new_text: str, insertion: str, position: Position, insert_index: int) -> str:
    """Inverse of `insert`: give back the original text exactly."""
    i, n = insert_index, len(insertion)
    if new_text[i : i + n] != insertion:
        raise InsertionError(f"insertion not found at index {i}")
    if position == "start":
        if i != 0 or new_text[n : n + len(SEP)] != SEP:
            raise InsertionError("start insertion not followed by the separator")
        return new_text[n + len(SEP) :]
    if position == "end":
        if i + n != len(new_text) or new_text[i - len(SEP) : i] != SEP:
            raise InsertionError("end insertion not preceded by the separator / not at the end")
        return new_text[: i - len(SEP)]
    if position == "middle":
        if new_text[i - len(SEP) : i] != SEP or new_text[i + n : i + n + len(SEP)] != SEP:
            raise InsertionError("middle insertion not surrounded by the separator")
        return new_text[: i - len(SEP)] + new_text[i + n + len(SEP) :]
    raise InsertionError(f"unknown position {position!r}")
