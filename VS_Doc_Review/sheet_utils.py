"""Small, boring helpers shared by the validators."""

import re
from typing import List

import config

_NOTE_MARKER_RE = re.compile(
    r"(?:^|\n)\s*" + re.escape(config.NOTE_MARKER) + r"\b",
    re.IGNORECASE,
)
# A blank line is how these cells separate "the actual command" from
# whatever comes after it (a note, a numbered list of setup steps, an
# alternate command for a different platform) - even when that trailing
# text doesn't happen to start with the word "Note".
_BLANK_LINE_RE = re.compile(r"\n\s*\n")


def col_letter_to_index(letter: str) -> int:
    """'A' -> 0, 'B' -> 1, ... 'Z' -> 25, 'AA' -> 26, etc."""
    index = 0
    for char in letter.strip().upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def cell(row: List[str], letter: str) -> str:
    """Reads a cell by column letter, tolerating short/ragged rows."""
    idx = col_letter_to_index(letter)
    if idx < len(row):
        return (row[idx] or "").strip()
    return ""


def sheet_row_number(zero_based_index_in_slice: int, data_start_row: int) -> int:
    """Turns 'the Nth data row (0-based)' into the real 1-indexed sheet row."""
    return data_start_row + zero_based_index_in_slice


def normalize(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def strip_notes(text: str) -> str:
    """
    Cuts off everything from the first paragraph break onward - whichever
    comes first: a blank line, or a literal "Note..." line. Columns F/G
    often carry human guidance below the real command - an alternate
    command for a different platform, a reminder to add an argument in a
    specific scenario, a numbered list of unrelated setup steps - and none
    of that is part of the actual command, so it shouldn't be scanned for
    arguments, IDs, or flags.
    """
    text = text or ""
    cut_points = []
    blank_line = _BLANK_LINE_RE.search(text)
    if blank_line:
        cut_points.append(blank_line.start())
    note_marker = _NOTE_MARKER_RE.search(text)
    if note_marker:
        cut_points.append(note_marker.start())
    if cut_points:
        return text[: min(cut_points)].strip()
    return text.strip()
