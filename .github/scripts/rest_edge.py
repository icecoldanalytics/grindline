#!/usr/bin/env python3
"""
Single source of truth for the Rest Edge signal: away team on a
back-to-back, home team rested exactly two days -> back the home side.

Signal 1 (home rested 3+ days) backtested at -2.0% ROI / 57.9% win rate
across 216 games at real closing moneylines (see backtest_rest_signals.py /
data/rest_signal_backtest.json) and has been retired - it is no longer
computed or logged anywhere in the live pipeline.

Every script that needs the rule itself, or the odds-to-profit math, should
import from here rather than re-deriving it. Each caller keeps its own
schedule-fetching (that legitimately differs by data source), and only
passes plain "days since last game" integers in here.
"""


def is_rest_edge(away_rest_days, home_rest_days):
    return away_rest_days == 1 and home_rest_days == 2


def is_cancelled(away_rest_days, home_rest_days):
    return away_rest_days == 1 and home_rest_days == 1


def home_ml_average(bookmakers, home_team):
    """Average American home moneyline across every bookmaker offering the
    h2h market in this odds snapshot (live or historical Odds API shape)."""
    prices = []
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for out in mkt.get("outcomes", []):
                if out.get("name") == home_team:
                    prices.append(out["price"])
    return round(sum(prices) / len(prices), 1) if prices else None


def profit(american, won):
    """Profit on a 100-unit stake at the given American price."""
    if not won:
        return -100.0
    return 100.0 * 100.0 / abs(american) if american < 0 else float(american)


def breakeven(american):
    """Win rate needed to break even at this price, as a percentage."""
    a = abs(american)
    return (a / (a + 100) * 100) if american < 0 else (100 / (american + 100) * 100)


def parse_odds_profit(american):
    """Returns (profit_if_win, risk) normalized to a $100-equivalent stake -
    the shape backtest_rest_signals.py's per-bet-return CI math wants."""
    if american > 0:
        return american, 100
    return 100, abs(american)
