#!/usr/bin/env python3
"""
Backtests retired Signal 1 (away team on a back-to-back, home team rested
3+ days), its two sub-buckets (home rested exactly 3 days vs. 4+ days -
the "dead zone" split), and Rest Edge (away team on a back-to-back, home
team rested exactly 2 days) across all four backfilled NHL seasons, graded
at real closing h2h moneylines - answering "would fading the tired away
team have made money, at the actual price on offer?" for each condition,
not an assumed -110.

Schedule (who played when, so back-to-back/rest can be derived) comes from
data/historical_h2h_events_cache.json - the raw Odds API discovery
snapshots backfill_h2h_odds.py already paid for - deduped by event id, so
it covers every discovered game, not just the subset priced into
data/historical_h2h_odds.json. Rest is only resolved up to "4+ days" (no
further lookback than 3 days prior); a team's rest at a season's first
few calendar days can undercount if its prior game fell outside the
discovery cache's window - a handful of games per season at most.

Results (final scores) are fetched fresh from the free NHL score API and
matched to events by date + common team name (e.g. "Predators" in
"Nashville Predators"), handling the Arizona Coyotes -> Utah Hockey Club ->
Utah Mammoth renames automatically since each snapshot only needs to match
against its own season's team names. Cached to SCORES_CACHE_PATH, resumable
like this repo's other NHL-fetch scripts (goalie_starts_cache.json etc).

Pricing: home moneyline averaged across every bookmaker offering the h2h
market in that game's snapshot (~20 min pre-puck-drop - the closing-line
proxy backfill_h2h_odds.py fetched), same convention as
capture_signals.py's home_prices().

Statistical method matches backtest_prop_model_full.py exactly: ROI is
total profit / total risk (dollar-weighted) across picks; the 95% CI is
built from the per-bet (pnl/risk*100) return distribution - mean +/- 1.96 *
(stdev / sqrt(n)).

Run:  python .github/scripts/backtest_rest_signals.py
Writes data/rest_signal_backtest.json.
"""
import json
import math
import os
import statistics
import time
from datetime import datetime, timedelta

import pytz
import requests

EASTERN = pytz.timezone("America/New_York")


def atomic_write_json(path, data, indent=None):
    """Write JSON via temp-file + rename so a process kill mid-write can't
    truncate the file on disk, matching backfill_h2h_odds.py's fix for the
    same non-atomic-save data-loss risk (see that script's docstring)."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


EVENTS_CACHE_PATH = "data/historical_h2h_events_cache.json"
ODDS_PATH = "data/historical_h2h_odds.json"
SCORES_CACHE_PATH = "data/rest_signal_scores_cache.json"
OUTPUT_PATH = "data/rest_signal_backtest.json"
REQUEST_SLEEP = 0.2

SEASONS = [
    ("2022-23", datetime(2022, 10, 7).date(), datetime(2023, 4, 14).date()),
    ("2023-24", datetime(2023, 10, 10).date(), datetime(2024, 4, 18).date()),
    ("2024-25", datetime(2024, 10, 4).date(), datetime(2025, 4, 17).date()),
    ("2025-26", datetime(2025, 10, 1).date(), datetime(2026, 4, 18).date()),
]


def local_game_date(commence_time_str):
    """The NHL's local (US/Eastern) calendar date for a game, matching
    backfill_h2h_odds.py so dates line up with data/historical_h2h_odds.json's keys."""
    utc_dt = datetime.strptime(commence_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
    return utc_dt.astimezone(EASTERN).date().strftime("%Y-%m-%d")


def season_for(date_str):
    for label, start, end in SEASONS:
        if start.strftime("%Y-%m-%d") <= date_str <= end.strftime("%Y-%m-%d"):
            return label
    return None


def load_events():
    """Every distinct discovered game, deduped by event id - same dedup
    backfill_h2h_odds.py's discover_events() does."""
    with open(EVENTS_CACHE_PATH) as f:
        cache = json.load(f)
    events_by_id = {}
    for events in cache.values():
        for e in events:
            events_by_id[e["id"]] = e
    return events_by_id


def build_all_dates():
    dates = []
    for _, start, end in SEASONS:
        d = start - timedelta(days=2)  # small buffer; harmless if no games
        while d <= end:
            dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    return dates


def fetch_scores_cache(dates):
    """Final scores per date from the NHL score API. Resumable: a rerun
    only fetches dates missing from SCORES_CACHE_PATH."""
    cache = {}
    try:
        with open(SCORES_CACHE_PATH) as f:
            cache = json.load(f)
    except FileNotFoundError:
        pass

    to_fetch = [d for d in dates if d not in cache]
    if to_fetch:
        print(f"Fetching NHL scores for {len(to_fetch)} new dates ({len(cache)} cached)...")
    for i, d in enumerate(to_fetch):
        try:
            r = requests.get(f"https://api-web.nhle.com/v1/score/{d}", timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  {d}: fetch error {e}")
            data = {"games": []}
        games = []
        for g in data.get("games", []):
            if g.get("gameState") not in ("OFF", "FINAL"):
                continue
            games.append({
                "away_common": g["awayTeam"]["name"]["default"],
                "home_common": g["homeTeam"]["name"]["default"],
                "away_score": g["awayTeam"].get("score", 0),
                "home_score": g["homeTeam"].get("score", 0),
            })
        cache[d] = games
        if (i + 1) % 40 == 0:
            print(f"  ...{i + 1}/{len(to_fetch)}")
            atomic_write_json(SCORES_CACHE_PATH, cache)
        time.sleep(REQUEST_SLEEP)
    atomic_write_json(SCORES_CACHE_PATH, cache)
    return cache


def match_score(scores_for_date, away_full, home_full):
    for g in scores_for_date:
        if g["away_common"] in away_full and g["home_common"] in home_full:
            return g["away_score"], g["home_score"]
    return None


def home_ml_avg(bookmakers, home_team):
    prices = []
    for bk in bookmakers:
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for out in mkt.get("outcomes", []):
                if out.get("name") == home_team:
                    prices.append(out["price"])
    return round(sum(prices) / len(prices), 1) if prices else None


def parse_odds_profit(american_odds):
    """Returns (profit_if_win, risk) normalized to $100-equivalent stake."""
    if american_odds > 0:
        return american_odds, 100
    return 100, abs(american_odds)


def calc_bucket(entries):
    n = len(entries)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": None, "roi": None, "roi_ci95": [None, None], "avg_odds": None}
    wins = sum(1 for e in entries if e["home_won"])
    total_profit, total_risk, per_bet = 0.0, 0.0, []
    for e in entries:
        profit_if_win, risk = parse_odds_profit(e["home_ml"])
        pnl = profit_if_win if e["home_won"] else -risk
        total_profit += pnl
        total_risk += risk
        per_bet.append(pnl / risk * 100)
    roi = total_profit / total_risk * 100
    if n > 1:
        se = statistics.stdev(per_bet) / math.sqrt(n)
        mean_ret = statistics.mean(per_bet)
        ci = [round(mean_ret - 1.96 * se, 1), round(mean_ret + 1.96 * se, 1)]
    else:
        ci = [None, None]
    avg_odds = sum(e["home_ml"] for e in entries) / n
    return {
        "n": n, "wins": wins, "win_rate": round(wins / n * 100, 1),
        "roi": round(roi, 1), "roi_ci95": ci, "avg_odds": round(avg_odds, 1),
    }


def fmt(label, r):
    if r["n"] == 0:
        print(f"  {label:<24} n=0")
        return
    ci = r["roi_ci95"]
    ci_str = f"[{ci[0]:+.1f}%, {ci[1]:+.1f}%]" if ci[0] is not None else "n/a"
    print(f"  {label:<24} n={r['n']:<5} wins={r['wins']:<5} win_rate={r['win_rate']:>5.1f}%  "
          f"avg={r['avg_odds']:+.1f}  roi={r['roi']:+.1f}%  95% CI={ci_str}")


def main():
    events_by_id = load_events()
    print(f"Loaded {len(events_by_id)} distinct discovered events.\n")

    with open(ODDS_PATH) as f:
        odds = json.load(f)

    events_by_date = {}
    for e in events_by_id.values():
        d = local_game_date(e["commence_time"])
        events_by_date.setdefault(d, []).append(e)

    teams_by_date = {d: set(e["home_team"] for e in evs) | set(e["away_team"] for e in evs)
                     for d, evs in events_by_date.items()}

    scores_cache = fetch_scores_cache(build_all_dates())

    retired_bucket = {"pooled": []}   # 3+ days, union of rest3 + rest4plus - kept for load_retired_signal1()
    rest3_bucket = {"pooled": []}     # exactly 3 days
    rest4plus_bucket = {"pooled": []} # 4 or more days
    rest_edge_bucket = {"pooled": []}
    for label, _, _ in SEASONS:
        retired_bucket[label] = []
        rest3_bucket[label] = []
        rest4plus_bucket[label] = []
        rest_edge_bucket[label] = []

    unpriced = {"signal1": 0, "rest3": 0, "rest4plus": 0, "rest_edge": 0}
    unscored = {"signal1": 0, "rest3": 0, "rest4plus": 0, "rest_edge": 0}

    for date_str, evs in sorted(events_by_date.items()):
        season = season_for(date_str)
        if season is None:
            continue
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        prev1 = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        prev2 = (d - timedelta(days=2)).strftime("%Y-%m-%d")
        prev3 = (d - timedelta(days=3)).strftime("%Y-%m-%d")

        for e in evs:
            away, home = e["away_team"], e["home_team"]
            away_b2b = away in teams_by_date.get(prev1, set())
            home_b2b = home in teams_by_date.get(prev1, set())
            if home_b2b:
                home_rest = 1
            elif home in teams_by_date.get(prev2, set()):
                home_rest = 2
            elif home in teams_by_date.get(prev3, set()):
                home_rest = 3
            else:
                home_rest = 4  # 4 or more days since home's last game

            if not away_b2b or home_b2b:
                continue  # cancelled (both B2B) or no signal

            if home_rest == 2:
                signal = "rest_edge"
            elif home_rest == 3:
                signal = "rest3"
            elif home_rest >= 4:
                signal = "rest4plus"
            else:
                continue

            odds_entry = odds.get(f"{date_str}|{away} @ {home}")
            ml = home_ml_avg(odds_entry.get("data", {}).get("bookmakers", []), home) if odds_entry else None
            if ml is None:
                unpriced[signal] += 1
                if signal in ("rest3", "rest4plus"):
                    unpriced["signal1"] += 1
                continue

            scored = match_score(scores_cache.get(date_str, []), away, home)
            if scored is None:
                unscored[signal] += 1
                if signal in ("rest3", "rest4plus"):
                    unscored["signal1"] += 1
                continue
            away_score, home_score = scored

            entry = {"date": date_str, "away": away, "home": home,
                     "home_ml": ml, "home_won": home_score > away_score}
            bucket = {"rest_edge": rest_edge_bucket, "rest3": rest3_bucket,
                      "rest4plus": rest4plus_bucket}[signal]
            bucket[season].append(entry)
            bucket["pooled"].append(entry)
            if signal in ("rest3", "rest4plus"):
                retired_bucket[season].append(entry)
                retired_bucket["pooled"].append(entry)

    print("\n" + "=" * 70)
    print("RETIRED SIGNAL 1 - Away B2B, Home Rested 3+ Days, back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(retired_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(retired_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['signal1']}, unscored: {unscored['signal1']})")

    print("\n" + "=" * 70)
    print("REST 3 EXACT - Away B2B, Home Rested Exactly 3 Days, back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(rest3_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(rest3_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['rest3']}, unscored: {unscored['rest3']})")

    print("\n" + "=" * 70)
    print("REST 4+ - Away B2B, Home Rested 4+ Days, back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(rest4plus_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(rest4plus_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['rest4plus']}, unscored: {unscored['rest4plus']})")

    print("\n" + "=" * 70)
    print("REST EDGE - Away B2B, Home Rested Exactly 2 Days, back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(rest_edge_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(rest_edge_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['rest_edge']}, unscored: {unscored['rest_edge']})")

    output = {
        "signal1_retired": {k: calc_bucket(v) for k, v in retired_bucket.items()},
        "rest3_exact": {k: calc_bucket(v) for k, v in rest3_bucket.items()},
        "rest4plus": {k: calc_bucket(v) for k, v in rest4plus_bucket.items()},
        "rest_edge": {k: calc_bucket(v) for k, v in rest_edge_bucket.items()},
        "unpriced": unpriced,
        "unscored": unscored,
        "method_notes": [
            "Schedule reconstructed from historical_h2h_events_cache.json event discovery, "
            "deduped by event id - covers every discovered game, not just priced ones.",
            "Results fetched fresh from the NHL score API, matched by date + common team name.",
            "Home moneyline is the average across every bookmaker offering h2h in the "
            "~20-min-pre-puck-drop snapshot backfill_h2h_odds.py fetched.",
            "ROI/CI method matches backtest_prop_model_full.py: dollar-weighted ROI, "
            "95% CI from the per-bet return distribution (mean +/- 1.96*SE).",
        ],
    }
    atomic_write_json(OUTPUT_PATH, output, indent=2)
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
