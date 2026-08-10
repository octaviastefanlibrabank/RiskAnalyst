"""Small shared helpers: step logging and safe numeric parsing."""
from __future__ import annotations

import re
import sys


class StepLogger:
    """Prints "[i/n] message" progress lines to the terminal, as requested in the spec."""

    def __init__(self, total: int):
        self.total = total
        self.current = 0

    def step(self, message: str) -> None:
        self.current += 1
        print(f"[{self.current}/{self.total}] {message}", file=sys.stderr)

    def info(self, message: str) -> None:
        print(f"      {message}", file=sys.stderr)

    def warn(self, message: str) -> None:
        print(f"      ! {message}", file=sys.stderr)


def safe_pct_change(current: float | None, previous: float | None) -> float | None:
    """Computes (current-previous)/previous safely. Never guesses missing inputs as 0."""
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / previous


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


_NUM_RE = re.compile(r"-?\d[\d.,]*")


def coerce_float(value) -> float | None:
    """Best-effort conversion of an LLM-extracted numeric-looking value to float.
    Returns None (never 0) if it cannot be parsed confidently."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if s in ("", "-", "N/A", "n/a"):
            return None
        m = _NUM_RE.search(s)
        if not m:
            return None
        token = m.group(0)
        # Normalize thousands/decimal separators: assume last separator = decimal if ambiguous
        token = token.replace(" ", "")
        if token.count(",") and token.count("."):
            token = token.replace(",", "")
        elif token.count(","):
            token = token.replace(",", ".")
        try:
            return float(token)
        except ValueError:
            return None
    return None
