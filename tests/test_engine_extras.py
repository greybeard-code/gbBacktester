import numpy as np
import pytest

from backtester.engine import Backtest
from backtester.sizing import carver_contracts
from backtester.strategy import Strategy

from conftest import MultiDayCatalog, make_day


def test_carver_contracts():
    # 50K * 15% / 16 = $468.75 daily vol target; MNQ pv=2:
    # ATR 100 pts -> $200/contract -> 2 contracts
    assert carver_contracts(50_000, 100.0, 2.0) == 2
    assert carver_contracts(50_000, 500.0, 2.0) == 1          # floor
    assert carver_contracts(50_000, 10.0, 2.0, max_contracts=5) == 5
    assert carver_contracts(50_000, 0.0, 2.0) == 1            # degenerate


class BuyOnce(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = None
    qty = 1

    def on_bar(self, bar, bars):
        if self.flat and bar.index == 0:
            self.buy()


def test_daily_loss_limit_flattens_and_stands_down():
    # bar 1: flat at 100; then decline 0.25/s for 4 minutes (-60 pts total)
    prices = [100.0] * 60 + [100.0 - 0.25 * i for i in range(1, 241)]
    day = make_day(prices)
    strat = BuyOnce()
    bt = Backtest(strat, prop=None, daily_loss_limit=30.0,
                  daily_limit_mode="kill", progress=False)
    bt.catalog = MultiDayCatalog([day])
    res = bt.run()
    assert res.dll_days == [day.date]
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_tag == "dll"
    # enforced tick by tick (2026-10-17; was bar-close, which let it run a
    # whole bar past the limit). make_day starts at t=1000 s, so bar 0 holds
    # events 0-19 and the buy fills on event 20 at the ask, 100.25 (price is
    # still 100). Balance 49,999.48 after commission, floor 49,970. Equity at
    # 85.75 is 49,970.48 (above), at 85.50 it is 49,969.98 -> flatten on that
    # event at the bid, 85.25
    assert t.entry_price == 100.25 and t.exit_price == 85.25
    assert t.exit_ts == int(day.ts[117])     # price 100 - 0.25 * 58 = 85.50
    assert t.pnl == pytest.approx((85.25 - 100.25) * 2 - 1.04)


def test_no_dll_when_not_hit():
    prices = [100.0] * 60 + [100.0 + 0.25 * (i % 4) for i in range(240)]
    day = make_day(prices)
    strat = BuyOnce()
    bt = Backtest(strat, prop=None, daily_loss_limit=500.0,
                  daily_limit_mode="kill", progress=False)
    bt.catalog = MultiDayCatalog([day])
    res = bt.run()
    assert res.dll_days == []
