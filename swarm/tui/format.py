"""Small formatting helpers shared by the screens."""

from __future__ import annotations

import time

from rich.markup import escape

GB = 1024**3

STATUS_STYLE = {
    "queued": "dim", "planning": "yellow", "running": "cyan", "paused": "magenta", "waiting_user": "bold yellow",
    "completed": "green", "failed": "red", "cancelled": "dim red", "pending": "dim", "ready": "dim cyan",
    "consensus": "blue", "skipped": "dim yellow", "created": "dim", "waiting_model": "yellow", "tool_call": "blue",
    "waiting_approval": "bold yellow", "loading": "yellow", "unloading": "dim", "converged": "green",
    "disagreement": "red", "weak": "yellow", "single": "dim",
}


def status(s: str | None) -> str:
    s = s or "-"
    return f"[{STATUS_STYLE.get(s, 'white')}]{escape(s)}[/]"


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


def bar(fraction: float, width: int = 20, color: str = "green") -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


def conf(c: float | None) -> str:
    if c is None:
        return "-"
    color = "green" if c >= 0.75 else "yellow" if c >= 0.5 else "red"
    return f"[{color}]{c:.2f}[/]"
