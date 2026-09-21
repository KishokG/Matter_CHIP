"""Small, boring helpers shared by the validators."""

from typing import List


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
