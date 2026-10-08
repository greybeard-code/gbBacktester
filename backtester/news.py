"""High-impact economic-event calendar for the news filter.

Loads the ForexFactory "red folder" dataset (data/ff_high_impact_news.csv,
gathered by tools/fetch_ff_news.py) and answers, for any bar timestamp,
whether it falls inside a pre/post window around a high-impact release.

Timestamps are int64 ns UTC everywhere (the engine's convention). The CSV's
`dateline_utc_epoch` column is ForexFactory's own UTC epoch (seconds), so no
timezone assumptions are made here.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

# repo-root/data/ff_high_impact_news.csv
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "data" / "ff_high_impact_news.csv"


class NewsCalendar:
    """Sorted event times with an O(log n) `blocked(ts)` window test."""

    def __init__(self, event_ns: np.ndarray):
        self._ns = np.asarray(event_ns, dtype="int64")
        self._ns.sort()

    def __len__(self) -> int:
        return int(self._ns.size)

    @classmethod
    def load(cls, csv_path=None, currencies=None, impacts=None) -> "NewsCalendar":
        """Build from the FF CSV.

        currencies: keep only these (case-insensitive), e.g. ("USD",). None =
        all. impacts: keep only these impact labels, e.g. ("high",). None =
        all rows in the file (the shipped file is high-impact only).
        """
        path = Path(csv_path) if csv_path else DEFAULT_CSV
        if not path.exists():
            raise FileNotFoundError(
                f"news calendar not found: {path}\n  "
                "gather it with: python tools/fetch_ff_news.py "
                "--start <YYYY-MM-DD> --end <YYYY-MM-DD>")
        cur = {c.upper() for c in currencies} if currencies else None
        imp = {i.lower() for i in impacts} if impacts else None
        secs: list[int] = []
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if cur and r.get("currency", "").upper() not in cur:
                    continue
                if imp and r.get("impact", "").lower() not in imp:
                    continue
                secs.append(int(r["dateline_utc_epoch"]))
        return cls(np.array(secs, dtype="int64") * 1_000_000_000)

    def blocked(self, ts_ns: int, pre_s: float, post_s: float) -> bool:
        """True if `ts_ns` is within [event - pre_s, event + post_s] of any
        event: i.e. some event lies in [ts - post_s, ts + pre_s]."""
        if self._ns.size == 0:
            return False
        lo = ts_ns - int(post_s * 1e9)
        hi = ts_ns + int(pre_s * 1e9)
        i = int(np.searchsorted(self._ns, lo, side="left"))
        return bool(i < self._ns.size and self._ns[i] <= hi)
