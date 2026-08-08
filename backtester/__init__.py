"""Tick-level futures backtester for NinjaTrader Market Replay Parquet data."""
from .account import PropFirmConfig
from .contracts import SPECS, ContractSpec, get_spec
from .engine import Backtest, Result
from .indicators import (ATR, EMA, KAMA, RSI, SMA, Bollinger,
                          EfficiencyRatio, Highest, KamaRegime, Lowest)
from .news import NewsCalendar
from .orders import BUY, SELL, Fill, Order, OrderType
from .sizing import carver_contracts
from .strategy import Bar, BarHistory, Strategy

__all__ = [
    "PropFirmConfig", "ATR", "Backtest", "Bar", "BarHistory", "Bollinger",
    "BUY",
    "carver_contracts",
    "ContractSpec", "EfficiencyRatio", "EMA", "Fill", "get_spec", "Highest",
    "KAMA", "KamaRegime", "Lowest", "NewsCalendar", "Order",
    "OrderType", "Result", "RSI", "SELL", "SMA", "SPECS", "Strategy",
]
