"""Small formatting helpers shared by the screens."""

from __future__ import annotations

import time

from rich.markup import escape

GB = 1024**3

# Colour only where attention is needed; everything else is plain or dim.
STATUS_STYLE = {
    "failed": "red", "cancelled": "dim", "waiting_user": "yellow", "waiting_approval": "yellow",
    "disagreement": "yellow", "weak": "yellow", "completed": "dim", "skipped": "dim", "queued": "dim",
    "pending": "dim", "ready": "dim", "created": "dim",
}


def status(s: str | None) -> str:
    s = s or "-"
    style = STATUS_STYLE.get(s)
    return f"[{style}]{escape(s)}[/]" if style else escape(s)


def gb(n: int | float | None, digits: int = 1) -> str:
    if not n:
        return "0 GB"
    return f"{n / GB:.{digits}f} GB"


def dur(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def ago(ts: float | None) -> str:
    if not ts:
        return "-"
    return dur(time.time() - ts) + " ago"


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0f}%"


def trunc(text: str | None, n: int) -> str:
    text = " ".join((text or "").split())
    return escape(text if len(text) <= n else text[: n - 1] + "…")


def conf(c: float | None) -> str:
    return "-" if c is None else f"{c:.2f}"
