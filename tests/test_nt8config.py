"""NT8 template parsers (backtester/nt8config.py) against real template files."""
from pathlib import Path

import pytest

from backtester.nt8config import (
    AtmBracket, AtmSpec, TrailStep, load_atm_template, load_strategy_template,
)



# -- ATM templates ----------------------------------------------------------





def _atm_xml(tmp_path, **overrides):
    fields = {"CalculationMode": "Ticks", "ReverseAtStop": "false"}
    fields.update(overrides)
    extra = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    p = tmp_path / "t.xml"
    p.write_text(
        "<NinjaTrader><AtmStrategy><Template>t</Template>"
        "<EntryQuantity>1</EntryQuantity>"
        "<Brackets><Bracket><Quantity>1</Quantity><StopLoss>10</StopLoss>"
        "<Target>10</Target></Bracket></Brackets>"
        f"{extra}</AtmStrategy></NinjaTrader>")
    return p


def test_atm_rejects_currency_mode(tmp_path):
    with pytest.raises(NotImplementedError, match="Currency"):
        load_atm_template(_atm_xml(tmp_path, CalculationMode="Currency"))


def test_atm_rejects_unmodeled_flags(tmp_path):
    with pytest.raises(NotImplementedError, match="ReverseAtStop"):
        load_atm_template(_atm_xml(tmp_path, ReverseAtStop="true"))




def test_exit_split_drops_empty_brackets():
    spec = AtmSpec(name="x", entry_qty=1, brackets=(
        AtmBracket(qty=2, stop_ticks=10, target_ticks=5),
        AtmBracket(qty=2, stop_ticks=10, target_ticks=15),
    ))
    split = spec.exit_split()
    assert [b.qty for b in split] == [1]
    assert split[0].target_ticks == 5


# -- Strategy templates -----------------------------------------------------



def test_strategy_template_saber_bar_spec(tmp_path):
    """SaberRenko's registered type id (20821) maps Value=bar size,
    BaseBarsPeriodValue=offset, Value2=time filter seconds — a different
    Value2 meaning than ninZaRenko's trend threshold (see
    research/SaberRenko_spec.md §7 Phase 4)."""
    p = tmp_path / "saber.xml"
    p.write_text(
        "<StrategyTemplate><StrategyType>X.Y.Z</StrategyType>"
        "<Strategy><SomeStrategy>"
        "<BarsPeriodSerializable>"
        "<BarsPeriodTypeSerialize>20821</BarsPeriodTypeSerialize>"
        "<BaseBarsPeriodType>Tick</BaseBarsPeriodType>"
        "<BaseBarsPeriodValue>16</BaseBarsPeriodValue>"
        "<Value>64</Value><Value2>1</Value2>"
        "</BarsPeriodSerializable>"
        "<InstrumentOrInstrumentList>MNQ 06-26</InstrumentOrInstrumentList>"
        "</SomeStrategy></Strategy></StrategyTemplate>")
    t = load_strategy_template(p)
    assert t.bar_spec == "s64-16-1"


def test_strategy_template_unknown_bar_type(tmp_path):
    p = tmp_path / "unknown.xml"
    p.write_text(
        "<StrategyTemplate><StrategyType>X.Y.Z</StrategyType>"
        "<Strategy><SomeStrategy>"
        "<BarsPeriodSerializable>"
        "<BarsPeriodTypeSerialize>999</BarsPeriodTypeSerialize>"
        "<Value>1</Value><Value2>0</Value2>"
        "</BarsPeriodSerializable>"
        "</SomeStrategy></Strategy></StrategyTemplate>")
    with pytest.raises(ValueError, match="not mapped to a BarSpec"):
        load_strategy_template(p)
