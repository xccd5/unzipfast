"""A tiny dependency-free progress bar."""

from __future__ import annotations

import sys
import time
from typing import TextIO

_BAR_WIDTH = 24
_LINE_WIDTH = 110


def human_size(value: float) -> str:
    """Format a byte count using binary units (KiB, MiB, ...)."""
    value = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PiB"  # pragma: no cover - unreachable


def human_duration(seconds: float) -> str:
    """Format an elapsed time for humans, e.g. ``1.24 s`` or ``2m 05.0s``."""
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60.0:
        return f"{seconds:.2f} s"
    minutes, rest = divmod(seconds, 60.0)
    if minutes < 60.0:
        return f"{int(minutes)}m {rest:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes:02d}m {int(rest):02d}s"


def format_eta(seconds: float) -> str:
    """Format a remaining time as ``mm:ss`` or ``h:mm:ss``."""
    total = max(0, int(round(seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class Progress:
    """Progress reporting for extraction and verification.

    The bar is only rendered when the output stream is a TTY, so piping the
    output of ``unzipfast`` never fills your logs with carriage returns.
    """

    def __init__(
        self,
        total_bytes: int,
        total_files: int,
        *,
        enabled: bool = True,
        stream: TextIO | None = None,
        min_interval: float = 0.1,
    ) -> None:
        self.total_bytes = max(int(total_bytes), 0)
        self.total_files = max(int(total_files), 0)
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled) and self.stream.isatty()
        self.min_interval = min_interval
        self.done_bytes = 0
        self.done_files = 0
        self._started = time.monotonic()
        self._last_render = 0.0
        self._rate = 0.0
        self._rate_bytes = 0
        self._rate_time = self._started
        self._finished = False

    def advance(self, nbytes: int = 0, nfiles: int = 0) -> None:
        self.done_bytes += max(int(nbytes), 0)
        self.done_files += max(int(nfiles), 0)
        if self.enabled and time.monotonic() - self._last_render >= self.min_interval:
            self._render()

    def _current_rate(self) -> float:
        now = time.monotonic()
        elapsed = now - self._rate_time
        if elapsed >= 0.25:
            instant = (self.done_bytes - self._rate_bytes) / elapsed
            self._rate = instant if self._rate == 0 else self._rate * 0.7 + instant * 0.3
            self._rate_bytes = self.done_bytes
            self._rate_time = now
        return self._rate

    def _fraction(self) -> float:
        if self.total_bytes > 0:
            return min(self.done_bytes / self.total_bytes, 1.0)
        if self.total_files > 0:
            return min(self.done_files / self.total_files, 1.0)
        return 1.0

    def _render(self) -> None:
        self._last_render = time.monotonic()
        fraction = self._fraction()
        filled = int(fraction * _BAR_WIDTH)
        bar = "=" * filled + "-" * (_BAR_WIDTH - filled)
        rate = self._current_rate()

        parts = [f"{fraction * 100:5.1f}%", f"[{bar}]"]
        if self.total_bytes > 0:
            parts.append(f"{human_size(self.done_bytes)} / {human_size(self.total_bytes)}")
        else:
            parts.append(f"{self.done_files:,} files")
        if rate > 0:
            parts.append(f"{human_size(rate)}/s")
            if self.total_bytes > self.done_bytes:
                parts.append("ETA " + format_eta((self.total_bytes - self.done_bytes) / rate))
        if self.total_files > 0:
            parts.append(f"{self.done_files:,}/{self.total_files:,}")

        line = "  " + "  ".join(parts)
        self.stream.write("\r" + line.ljust(_LINE_WIDTH))
        self.stream.flush()

    def finish(self) -> None:
        """Draw the final state and move to the next line."""
        if not self.enabled or self._finished:
            return
        self._finished = True
        self._render()
        self.stream.write("\n")
        self.stream.flush()

    # Convenience alias so the object can be used as a context manager.
    close = finish

    def __enter__(self) -> "Progress":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.finish()
