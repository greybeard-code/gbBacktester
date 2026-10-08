"""Worked example: an EMA-cross that obeys the Apex risk helpers.

This is a DEMONSTRATION of ``Strategy.size_within_budget()`` and
``Strategy.bank_profit()`` (see ``backtester/risk.py``) — NOT a validated edge,
it exists to show the two calls a prop-firm strategy makes every bar:

  * size to fit   — ``size_within_budget(stop_ticks)`` returns the largest size
                    whose stop fits ``risk_max_loss_frac`` of the *live* Apex
                    headroom (equity - trailing floor), or 0 to stand down;
  * bank the peak — ``bank_profit(price)`` closes at the account profit target
                    (``eval_target``) or ratchets the stop behind the unrealized
                    high-water mark, because the Apex intraday floor trails
                    unrealized gains and never recedes.

Configured for the active account — a **25K Apex Intraday** evaluation (target
$1,500, trailing drawdown $1,000, cap 40 MNQ). Run with the matching prop
config, e.g.::

    python cli.py strategies/apex_budget_ema.py --symbol MNQ --period 1m \\
        --start 2026-06-01 --end 2026-06-17 --balance 25000 \\
        --prop-threshold 1000 --mc-target 1500

(For exact eval semantics the trailing floor would freeze at the profit-target
balance — ``risk.APEX_INTRADAY[25_000].config()`` — the CLI's default
``lock_buffer`` is close enough for a demonstration.)
"""
from backtester import EMA, Strategy


class ApexBudgetEma(Strategy):
    symbol = "MNQ"
    period = "1m"
    session = ("09:30", "16:00")     # RTH, US/Eastern
    flat_at_session_end = True

    # --- 25K Apex Intraday account ---
    max_position = 40                # 25K cap: 40 MNQ
    eval_target = 1_500.0            # pass target -> bank_profit() closes here

    # --- risk knobs (see backtester/risk.py) ---
    risk_max_loss_frac = 0.5         # a trade risks <= half the live headroom
    profit_bank_arm_frac = 0.5       # arm the peak-lock at half the target
    profit_bank_giveback = 0.4       # give back <= 40% of the open peak

    # --- signal ---
    fast_period = 9
    slow_period = 21
    stop_ticks = 40                  # 10 pts on MNQ
    target_ticks = 80                # 20 pts

    def on_start(self):
        self.fast = EMA(self.fast_period)
        self.slow = EMA(self.slow_period)
        self.prev_diff = None

    def on_bar(self, bar, bars):
        f = self.fast.update(bar.close)
        s = self.slow.update(bar.close)

        # In a position: protect the unrealized peak every bar. Closes at the
        # eval target, else ratchets the stop so <= giveback of the peak is
        # given back (the Apex floor only ratchets up, never down).
        if not self.flat:
            self.bank_profit(price=bar.close)
            return

        if not self.slow.ready:
            return
        diff = f - s
        if self.prev_diff is None:
            self.prev_diff = diff
            return

        side = 0
        if self.prev_diff <= 0 < diff:
            side = 1
        elif self.prev_diff >= 0 > diff:
            side = -1
        self.prev_diff = diff
        if side == 0:
            return

        # Size to the live Apex headroom. 0 = the stop budget can't support a
        # single contract -> stand down rather than risk the trailing floor.
        n = self.size_within_budget(self.stop_ticks, price=bar.close)
        if n <= 0:
            return
        if side > 0:
            self.buy_bracket(qty=n, stop_ticks=self.stop_ticks,
                             target_ticks=self.target_ticks, tag="long")
        else:
            self.sell_bracket(qty=n, stop_ticks=self.stop_ticks,
                              target_ticks=self.target_ticks, tag="short")
