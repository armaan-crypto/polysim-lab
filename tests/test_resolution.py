import random
from pathlib import Path

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from pmsim.book.units import CASH_SCALE, PRICE_SCALE, SIZE_SCALE
from pmsim.config import ResolutionConfig, ResolutionRisk, load_config
from pmsim.engine.fills import Fill
from pmsim.engine.portfolio import Portfolio
from pmsim.risk.negrisk import GroupMarket, NegRiskGroup, groups_from_catalog
from pmsim.risk.resolution import (
    HOUR_MS,
    Resolution,
    binary_payouts,
    draw_resolution,
    group_payouts,
    market_class,
    risk_profile,
    settle,
    simulate_binary,
    simulate_group,
)

ROOT = Path(__file__).resolve().parents[1]
S = SIZE_SCALE
GROUP = NegRiskGroup(
    "g",
    tuple(GroupMarket(f"m{i}", f"o{i}", f"y{i}", f"n{i}", False) for i in range(3)),
)


def buy(pf: Portfolio, token: str, price: int, size: int) -> None:
    pf.apply_fill(Fill(1, token, "BUY", price, size, pf.now, "taker", 0, 0))


# ---- payouts and settlement ---------------------------------------------------


def test_binary_payouts() -> None:
    assert binary_payouts("y", "n", "winner", True) == {"y": PRICE_SCALE, "n": 0}
    assert binary_payouts("y", "n", "winner", False) == {"y": 0, "n": PRICE_SCALE}
    assert binary_payouts("y", "n", "fifty_fifty", True) == {"y": 5000, "n": 5000}
    assert binary_payouts("y", "n", "void", True) == {}


def test_group_payouts_one_winner() -> None:
    pay = group_payouts(GROUP, 1)
    assert [pay[f"y{i}"] for i in range(3)] == [0, PRICE_SCALE, 0]
    assert [pay[f"n{i}"] for i in range(3)] == [PRICE_SCALE, 0, PRICE_SCALE]


def test_settle_redeems_and_refunds() -> None:
    pf = Portfolio(cash=10 * CASH_SCALE)
    buy(pf, "y", 9000, 10 * S)
    buy(pf, "v", 5000, 2 * S)
    settle(pf, Resolution(100, "winner", {"y": PRICE_SCALE, "n": 0}, ("v",)))
    assert pf.cash == 11 * CASH_SCALE and pf.size("y") == 0 and pf.size("v") == 0


def test_settle_charges_redeem_cost() -> None:
    pf = Portfolio(cash=CASH_SCALE)
    buy(pf, "y", 5000, S)
    settle(pf, Resolution(1, "winner", {"y": PRICE_SCALE}), redeem_cost=CASH_SCALE // 100)
    assert pf.cash == CASH_SCALE + CASH_SCALE // 2 - CASH_SCALE // 100


@given(st.integers(0, 2), st.integers(0, 2), st.integers(1, 30))
def test_conversion_preserves_payoff(no_idx: int, winner: int, size: int) -> None:
    """Holding NO_i pays the same as holding YES on every other outcome."""
    pay = group_payouts(GROUP, winner)
    no_value = pay[GROUP.markets[no_idx].no]
    yes_value = sum(pay[t] for t in GROUP.other_yes(no_idx))
    assert no_value == yes_value


# ---- risk simulation ------------------------------------------------------------


def test_clean_resolution_takes_challenge_period() -> None:
    risk = ResolutionRisk(p_late_proposal=0, p_dispute=0, p_fifty_fifty=0)
    res = simulate_binary(random.Random(1), "y", "n", True, 1000, risk)
    assert res.ts == 1000 + 2 * HOUR_MS and res.payouts == {"y": PRICE_SCALE, "n": 0}


def test_dispute_reversal_and_delay() -> None:
    risk = ResolutionRisk(p_late_proposal=0, p_dispute=1, p_reversal=1, p_fifty_fifty=0)
    res = simulate_binary(random.Random(1), "y", "n", True, 0, risk)
    assert res.disputed and res.reversed and res.payouts["n"] == PRICE_SCALE
    assert res.ts == 120 * HOUR_MS


def test_neg_risk_never_splits() -> None:
    risk = ResolutionRisk(p_fifty_fifty=1, p_dispute=0)
    res = simulate_group(random.Random(1), GROUP, 2, 0, risk)
    assert res.kind == "winner" and res.payouts["y2"] == PRICE_SCALE


def test_fifty_fifty_and_void_binary() -> None:
    ff = simulate_binary(random.Random(1), "y", "n", True, 0, ResolutionRisk(p_fifty_fifty=1))
    assert ff.kind == "fifty_fifty" and ff.payouts == {"y": 5000, "n": 5000}
    void = simulate_binary(random.Random(1), "y", "n", True, 0, ResolutionRisk(p_void=1))
    assert void.kind == "void" and void.voided == ("y", "n")
    gv = simulate_group(random.Random(1), GROUP, 0, 0, ResolutionRisk(p_void=1))
    assert len(gv.voided) == 6


def test_draws_are_seeded_and_rates_match() -> None:
    risk = ResolutionRisk(p_dispute=0.1, p_reversal=0.5)
    a = [draw_resolution(random.Random(7), 0, 2, risk, True) for _ in range(3)]
    assert a[0] == a[1] == a[2]
    rng = random.Random(3)
    draws = [draw_resolution(rng, 0, 2, risk, True) for _ in range(20_000)]
    assert sum(d.disputed for d in draws) / len(draws) == pytest.approx(0.1, abs=0.01)
    assert sum(d.reversed for d in draws) / len(draws) == pytest.approx(0.05, abs=0.01)


# ---- market classes and config ----------------------------------------------------


def test_class_profiles_override_default() -> None:
    cfg = load_config(ROOT / "config" / "default.yaml").resolution
    assert market_class(["nba", "sports"], cfg) == "sports"
    assert market_class(["weather"], cfg) == "default"
    sports = risk_profile(["sports"], cfg)
    assert sports.p_dispute == 0.01 and sports.dispute_extra_hours == 118
    assert risk_profile(["x"], ResolutionConfig()) == ResolutionRisk()


# ---- neg-risk groups ------------------------------------------------------------------


def test_group_helpers() -> None:
    prices = {"y0": 5000, "y1": 3000, "y2": 2500}
    assert GROUP.yes_sum(prices) == 10_500
    assert sum(GROUP.implied_probabilities(prices)) == pytest.approx(1.0)
    assert GROUP.other_yes(1) == ["y0", "y2"]
    assert GROUP.conversion_value(0, prices) == 5500
    assert GROUP.index_of("n2") == 2
    with pytest.raises(KeyError):
        GROUP.index_of("zz")


def test_groups_from_catalog() -> None:
    rows = []
    for g in ("G1", "G2"):
        for i in range(3 if g == "G1" else 2):
            for idx, out in enumerate(("Yes", "No")):
                rows.append(
                    {
                        "market_id": f"{g}m{i}",
                        "token_id": f"{g}{i}{out}",
                        "outcome_index": idx,
                        "neg_risk": True,
                        "neg_risk_market_id": g,
                        "group_item_title": f"t{i}",
                        "neg_risk_other": i == 2,
                    }
                )
    rows.append(
        {
            "market_id": "b",
            "token_id": "bY",
            "outcome_index": 0,
            "neg_risk": False,
            "neg_risk_market_id": "",
            "group_item_title": "",
            "neg_risk_other": False,
        }
    )
    groups = groups_from_catalog(pl.DataFrame(rows))
    assert [len(g.markets) for g in groups] == [3, 2]
    g1 = groups[0]
    assert g1.markets[0].yes == "G10Yes" and g1.markets[0].no == "G10No"
    assert g1.markets[2].is_other
