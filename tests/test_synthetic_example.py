"""examples/make_synthetic_data.py must keep producing data the Catalog reads."""
import importlib.util
from datetime import date
from pathlib import Path

import numpy as np

from backtester.data import Catalog

_spec = importlib.util.spec_from_file_location(
    "make_synthetic_data",
    Path(__file__).resolve().parent.parent / "examples" / "make_synthetic_data.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_synthetic_days_load_and_are_sane(tmp_path):
    import pyarrow.parquet as pq

    rng = np.random.default_rng(1)
    folder = tmp_path / "data" / "2026" / "MNQ-2026_L1"
    folder.mkdir(parents=True)
    pq.write_table(_mod.make_day(date(2026, 6, 1), rng, 0.25, 20000.0, 500),
                   folder / "20260601.parquet")

    cat = Catalog(tmp_path / "data", tmp_path / "cache")
    assert cat.days("MNQ") == ["20260601"]
    day = cat.load_day("MNQ", "20260601")
    assert len(day.price) == 500
    assert np.all(np.diff(day.ts) >= 0)
    assert np.all(day.bid < day.ask)                 # never crossed
    assert np.all((day.price == day.bid) | (day.price == day.ask))
    # 09:30 ET on 2026-06-01 is 13:30 UTC (EDT)
    first_utc_hour = (day.ts[0] // 3_600_000_000_000) % 24
    assert first_utc_hour in (13, 14, 15, 16, 17)
