"""Parallel-lanes TUI for concurrent fetch/summarize/caption stages.

One *lane* per in-flight item (link or image). Deltas are routed by a stable
id (a link's url, an image's md5), so streaming text from N concurrent workers
lands in N separate buffers and never interleaves — the user's tag-by-id idea.
Completed-OK items leave the view (counted in the tally); **failures persist**
as ``✗ label — reason`` lines so they stay visible for the rest of the stage.

Thread-safety by construction: worker threads only call the reporter methods
(:meth:`Lanes.start` / :meth:`phase` / :meth:`delta` / :meth:`done`), which
mutate per-id state under a lock. Rendering reads that state under the same lock
inside ``__rich__``; nothing but the Live refresh thread renders. Drive it with::

    lanes = Lanes("链接增强", total=n, subtitle=model, status_labels={...})
    with Live(lanes, console=console, refresh_per_second=12, transient=False):
        run_workers(reporter=lanes)   # workers call lanes.start/phase/delta/done
        lanes.freeze()                # final frame: spinner → ✓, keep failures
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from rich.console import Group
from rich.text import Text

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_WIDTH = 52


@dataclass
class _Lane:
    label: str
    phase: str = ""
    chars: int = 0
    tail: str = ""
    error: str | None = None


class Lanes:
    """A live, thread-safe parallel-lanes renderable + worker reporter.

    *status_labels* maps the status strings passed to :meth:`done` to the
    Chinese words shown in the footer tally (e.g.
    ``{"summary": "摘要", "short": "太短", "failed": "失败"}``). The ``"failed"``
    status is special: those lanes are kept as persistent ✗ lines.
    """

    def __init__(
        self,
        title: str,
        total: int,
        *,
        subtitle: str = "",
        max_active: int = 5,
        tail_chars: int = 30,
        status_labels: dict[str, str] | None = None,
    ) -> None:
        self.title = title
        self.total = total
        self.subtitle = subtitle
        self.max_active = max_active
        self.tail_chars = tail_chars
        self.status_labels = status_labels or {}
        self._lanes: dict[str, _Lane] = {}
        self._order: list[str] = []
        self._failures: list[_Lane] = []
        self._tally: dict[str, int] = {}
        self._done = 0
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._frozen = False

    # ── reporter API (called from worker threads) ────────────────────────────
    def start(self, uid: str, label: str) -> None:
        with self._lock:
            if uid not in self._lanes:
                self._order.append(uid)
            self._lanes[uid] = _Lane(label=label)

    def phase(self, uid: str, phase: str) -> None:
        with self._lock:
            if (lane := self._lanes.get(uid)) is not None:
                lane.phase = phase

    def delta(self, uid: str, text: str) -> None:
        with self._lock:
            if (lane := self._lanes.get(uid)) is not None:
                lane.chars += len(text)
                lane.tail = (lane.tail + text).replace("\n", " ")[-self.tail_chars:]

    def done(self, uid: str, status: str, *, error: str | None = None) -> None:
        with self._lock:
            lane = self._lanes.pop(uid, None)
            if uid in self._order:
                self._order.remove(uid)
            self._done += 1
            self._tally[status] = self._tally.get(status, 0) + 1
            if status == "failed":
                fail = lane or _Lane(label=uid)
                fail.error = error or "失败"
                self._failures.append(fail)

    def freeze(self) -> None:
        with self._lock:
            self._frozen = True

    # ── rendering (called from the Live refresh thread) ──────────────────────
    def __rich__(self) -> Group:
        with self._lock:
            elapsed = time.monotonic() - self._t0
            glyph = "✓" if self._frozen else _SPIN[int(elapsed * 12) % len(_SPIN)]

            # Every row is forced to a single ellipsized line so a long summary
            # tail can never wrap and shove the layout around.
            def _line(style: str = "") -> Text:
                return Text(style=style, no_wrap=True, overflow="ellipsis")

            rows: list[Text] = []
            head = _line()
            head.append(f"{self.title} ", style="bold")
            if self.subtitle:
                head.append(f"({self.subtitle}) ", style="dim")
            head.append(f"  {self._done}/{self.total}", style="cyan")
            rows.append(head)
            rows.append(_line("dim").append("─" * _WIDTH))

            active = [self._lanes[u] for u in self._order][: self.max_active]
            for lane in active:
                row = _line()
                row.append(f"{glyph} ", style="cyan")
                row.append(_clip(lane.label, 22))
                if lane.phase:
                    row.append(f"  {lane.phase}", style="yellow")
                if lane.chars:
                    row.append(f" {lane.chars}字", style="dim")
                if lane.tail:
                    row.append(f" ▏{lane.tail}", style="dim italic")
                rows.append(row)
            if not active and not self._frozen:
                rows.append(_line("dim").append("  …"))

            if self._failures:
                rows.append(_line("red dim").append("─" * _WIDTH))
                for fail in self._failures[-6:]:
                    row = _line()
                    row.append("✗ ", style="bold red")
                    row.append(_clip(fail.label, 24))
                    row.append(f" — {fail.error}", style="red")
                    rows.append(row)

            foot = _line()
            tally = "  ".join(
                f"{n} {self.status_labels.get(k, k)}" for k, n in self._tally.items()
            )
            foot.append(tally or "处理中…", style="dim")
            foot.append(f"    ⏱ {int(elapsed) // 60}:{int(elapsed) % 60:02d}", style="dim")
            rows.append(_line("dim").append("─" * _WIDTH))
            rows.append(foot)
            return Group(*rows)


def _clip(text: str, limit: int) -> str:
    """Trim *text* to roughly *limit* display columns (CJK counts as 2)."""
    width = 0
    out: list[str] = []
    for ch in text:
        w = 2 if ord(ch) > 0x2E7F else 1
        if width + w > limit:
            out.append("…")
            break
        width += w
        out.append(ch)
    return "".join(out)
