#!/usr/bin/env python3
"""
Backtests the goalie condition (Rest Edge - away team on a back-to-back,
home team rested exactly 2 days - AND the away team started its own #1
goalie by cumulative starts, judged only on starts entering that date)
across all four backfilled NHL seasons, graded at real closing h2h
moneylines. Also reports the complement bucket (away started a backup)
for comparison, matching backtest_rest_signals.py's pattern of reporting
a comparison bucket alongside the signal itself.

This exists because data/goalie_research.json's 63.8% win rate / +9.8%
ROI claim has no generator anywhere in this repo (git log shows it was
added whole, by hand, in a single commit with no supporting code or
cache files) - its own "real home moneyline" claim is unverifiable from
this repo. This script is the real thing.

Schedule and pricing reuse backtest_rest_signals.py's exact sources and
methodology: data/historical_h2h_events_cache.json for the schedule
(deduped by event id), data/historical_h2h_odds.json for real closing
h2h prices, the same home_rest/away_b2b derivation, and the identical
ROI/CI statistical method (dollar-weighted ROI, 95% CI from the per-bet
return distribution).

Starting goalies are NOT already available for all four seasons -
data/goalie_starts_cache.json (built by backtest_signal2_history.py)
only covers 2025-26. This script fetches box scores for every game in
all four seasons fresh (resumable, cached to GOALIE_CACHE_PATH), because
determining "is this team's #1 goalie" requires knowing who started
EVERY game that team played this season, not just its Rest Edge games -
same reconstruction backtest_signal2_history.py does for one season,
extended to four and keyed to match this repo's odds/events convention
(f"{date}|{away_full} @ {home_full}") instead of by abbreviation, so it
can be looked up directly against the same events used for pricing.

Backup definition (identical to backtest_signal2_history.py): a starter
has strictly fewer cumulative starts than the team's leader entering
that date, AND the team has played at least MIN_TEAM_GAMES games so far
THIS SEASON (avoids early-season noise before a clear #1 exists).
Cumulative starts reset at each season boundary - a team's #1 goalie one
season says nothing about who it is the next.

Run:  python .github/scripts/backtest_goalie_signal.py
(Long-running: ~5,270 boxscore fetches across four seasons, resumable -
safe to Ctrl-C and rerun. Writes data/goalie_signal_backtest.json.)
"""
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta

import pytz
import requests

EASTERN = pytz.timezone("America/New_York")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def atomic_write_json(path, data, indent=None):
    """Write JSON via temp-file + rename so a process kill mid-write can't
    truncate the file on disk - see backfill_h2h_odds.py's docstring for
    why this matters (a background run corrupted a prior data file this
    same way)."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


def load_json_or_fail(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"\n{path} exists but will not parse as JSON ({e}). Refusing to "
            f"continue - check {path}.bak before rerunning."
        )


EVENTS_CACHE_PATH = "data/historical_h2h_events_cache.json"
ODDS_PATH = "data/historical_h2h_odds.json"
SCORES_CACHE_PATH = "data/rest_signal_scores_cache.json"
GOALIE_CACHE_PATH = "data/goalie_starts_backtest_cache.json"
OUTPUT_PATH = "data/goalie_signal_backtest.json"
REQUEST_SLEEP = 0.2
SAVE_EVERY = 40
MIN_TEAM_GAMES = 10  # matches backtest_signal2_history.py's MIN_TEAM_GAMES_FOR_BACKUP_CALL

SEASONS = [
    ("2022-23", datetime(2022, 10, 7).date(), datetime(2023, 4, 14).date()),
    ("2023-24", datetime(2023, 10, 10).date(), datetime(2024, 4, 18).date()),
    ("2024-25", datetime(2024, 10, 4).date(), datetime(2025, 4, 17).date()),
    ("2025-26", datetime(2025, 10, 1).date(), datetime(2026, 4, 18).date()),
]


def local_game_date(commence_time_str):
    utc_dt = datetime.strptime(commence_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
    return utc_dt.astimezone(EASTERN).date().strftime("%Y-%m-%d")


def season_for(date_str):
    for label, start, end in SEASONS:
        if start.strftime("%Y-%m-%d") <= date_str <= end.strftime("%Y-%m-%d"):
            return label
    return None


def load_events():
    cache = load_json_or_fail(EVENTS_CACHE_PATH)
    events_by_id = {}
    for events in cache.values():
        for e in events:
            events_by_id[e["id"]] = e
    return events_by_id


def parse_toi(toi_str):
    try:
        m, s = toi_str.split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def get_score_data(date_str):
    """Today's NHL games with their own NHL game id (needed for the
    boxscore fetch) plus common team names (for matching to the Odds-API
    full-name events)."""
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/score/{date_str}", timeout=15)
        r.raise_for_status()
        return r.json().get("games", [])
    except Exception as e:
        print(f"  score fetch error {date_str}: {e}")
        return []


def get_starters(game_id):
    """Return (away_starter_name, home_starter_name) from the boxscore -
    identical logic to backtest_signal2_history.py's get_starters()."""
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore", timeout=15)
        r.raise_for_status()
        box = r.json()
    except Exception as e:
        print(f"  boxscore error for game {game_id}: {e}")
        return "", ""

    pbs = box.get("playerByGameStats", {})
    result = {}
    for side in ("awayTeam", "homeTeam"):
        goalies = pbs.get(side, {}).get("goalies", [])
        starter = None
        for g in goalies:
            if g.get("starter") is True:
                starter = g
                break
        if starter is None and goalies:
            starter = max(goalies, key=lambda g: parse_toi(g.get("toi", "0:00")))
        result[side] = starter.get("name", {}).get("default", "") if starter else ""
    return result.get("awayTeam", ""), result.get("homeTeam", "")


def fetch_goalie_starts(events_by_date):
    """Resumable: keyed by f"{date}|{away_full} @ {home_full}" to match
    this repo's odds-lookup convention directly - no abbreviation lookup
    needed at use time, only here at fetch time."""
    cache = {}
    if os.path.exists(GOALIE_CACHE_PATH):
        cache = load_json_or_fail(GOALIE_CACHE_PATH)
        print(f"Resuming goalie starts: {len(cache)} games already fetched.")

    to_fetch_dates = sorted(events_by_date.keys())
    fetched_since_save = 0
    total_new = 0

    for di, date_str in enumerate(to_fetch_dates):
        day_events = events_by_date[date_str]
        needed = [e for e in day_events
                  if f"{date_str}|{e['away_team']} @ {e['home_team']}" not in cache]
        if not needed:
            continue

        day_games = get_score_data(date_str)
        time.sleep(REQUEST_SLEEP)

        for e in needed:
            key = f"{date_str}|{e['away_team']} @ {e['home_team']}"
            match = None
            for g in day_games:
                if g.get("gameState") not in ("OFF", "FINAL"):
                    continue
                away_common = g["awayTeam"]["name"]["default"]
                home_common = g["homeTeam"]["name"]["default"]
                if away_common in e["away_team"] and home_common in e["home_team"]:
                    match = g
                    break
            if match is None:
                cache[key] = {"away_starter": "", "home_starter": ""}
                continue

            away_starter, home_starter = get_starters(match["id"])
            time.sleep(REQUEST_SLEEP)
            cache[key] = {"away_starter": away_starter, "home_starter": home_starter}
            total_new += 1
            fetched_since_save += 1

            if fetched_since_save >= SAVE_EVERY:
                atomic_write_json(GOALIE_CACHE_PATH, cache)
                fetched_since_save = 0
                print(f"  ...progress saved: {total_new} new games fetched this run "
                      f"({len(cache)} total cached)")

        if (di + 1) % 30 == 0:
            print(f"  ...date progress: {di + 1}/{len(to_fetch_dates)}")

    atomic_write_json(GOALIE_CACHE_PATH, cache)
    print(f"Goalie starts fetch complete: {total_new} new this run, {len(cache)} total cached.\n")
    return cache


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

    events_by_date = {}
    for e in events_by_id.values():
        d = local_game_date(e["commence_time"])
        if season_for(d) is None:
            continue
        events_by_date.setdefault(d, []).append(e)

    teams_by_date = {d: set(e["home_team"] for e in evs) | set(e["away_team"] for e in evs)
                     for d, evs in events_by_date.items()}

    odds = load_json_or_fail(ODDS_PATH)
    scores_cache = load_json_or_fail(SCORES_CACHE_PATH)
    goalie_cache = fetch_goalie_starts(events_by_date)

    def match_score(scores_for_date, away_full, home_full):
        for g in scores_for_date:
            if g["away_common"] in away_full and g["home_common"] in home_full:
                return g["away_score"], g["home_score"]
        return None

    # Walk chronologically, tracking cumulative starts per team PER SEASON
    # (reset at each season boundary), exactly like backtest_signal2_history.py.
    starts_by_season = {label: {} for label, _, _ in SEASONS}     # team -> {starter: count}
    games_played_by_season = {label: {} for label, _, _ in SEASONS}  # team -> count

    number_one_bucket = {"pooled": []}
    backup_bucket = {"pooled": []}
    for label, _, _ in SEASONS:
        number_one_bucket[label] = []
        backup_bucket[label] = []

    unpriced = {"number_one": 0, "backup": 0}
    unscored = {"number_one": 0, "backup": 0}
    no_starter_data = 0

    for date_str in sorted(events_by_date.keys()):
        season = season_for(date_str)
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        prev1 = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        prev2 = (d - timedelta(days=2)).strftime("%Y-%m-%d")

        starts = starts_by_season[season]
        team_games = games_played_by_season[season]

        for e in sorted(events_by_date[date_str], key=lambda e: e["id"]):
            away, home = e["away_team"], e["home_team"]
            away_b2b = away in teams_by_date.get(prev1, set())
            home_b2b = home in teams_by_date.get(prev1, set())
            if home_b2b:
                home_rest = 1
            elif home in teams_by_date.get(prev2, set()):
                home_rest = 2
            else:
                home_rest = 3

            key = f"{date_str}|{away} @ {home}"
            starter_info = goalie_cache.get(key, {})
            away_starter = starter_info.get("away_starter", "")

            is_signal = away_b2b and not home_b2b and home_rest == 2

            if is_signal and away_starter:
                gp = team_games.get(away, 0)
                team_starts = starts.get(away, {})
                backup_start = False
                if gp >= MIN_TEAM_GAMES and team_starts:
                    leader_starts = max(team_starts.values())
                    starter_starts = team_starts.get(away_starter, 0)
                    backup_start = starter_starts < leader_starts
                # Games before MIN_TEAM_GAMES: no clear #1 exists yet, so
                # this game is excluded from both buckets rather than
                # guessed at (matches backtest_signal2_history.py's stance).
                if gp >= MIN_TEAM_GAMES:
                    signal = "backup" if backup_start else "number_one"

                    odds_entry = odds.get(key)
                    ml = home_ml_avg(odds_entry.get("data", {}).get("bookmakers", []), home) if odds_entry else None
                    if ml is None:
                        unpriced[signal] += 1
                    else:
                        scored = match_score(scores_cache.get(date_str, []), away, home)
                        if scored is None:
                            unscored[signal] += 1
                        else:
                            away_score, home_score = scored
                            entry = {"date": date_str, "away": away, "home": home,
                                     "home_ml": ml, "home_won": home_score > away_score,
                                     "starter": away_starter}
                            bucket = number_one_bucket if signal == "number_one" else backup_bucket
                            bucket[season].append(entry)
                            bucket["pooled"].append(entry)
            elif is_signal and not away_starter:
                no_starter_data += 1

            # Update cumulative tracking AFTER evaluating (no hindsight),
            # for every game (not just signal games) - a team's #1 is
            # determined by its whole season, not just its B2B nights.
            for team, starter in ((away, away_starter), (home, starter_info.get("home_starter", ""))):
                team_games[team] = team_games.get(team, 0) + 1
                if starter:
                    starts.setdefault(team, {})
                    starts[team][starter] = starts[team].get(starter, 0) + 1

    print("\n" + "=" * 70)
    print("GOALIE SIGNAL - Rest Edge + Away Started Its #1 Goalie, back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(number_one_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(number_one_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['number_one']}, unscored: {unscored['number_one']})")

    print("\n" + "=" * 70)
    print("COMPARISON - Rest Edge + Away Started a Backup, back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(backup_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(backup_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['backup']}, unscored: {unscored['backup']})")

    print(f"\n  Rest Edge games excluded for missing starter data: {no_starter_data}")

    output = {
        "goalie_number_one": {k: calc_bucket(v) for k, v in number_one_bucket.items()},
        "goalie_backup": {k: calc_bucket(v) for k, v in backup_bucket.items()},
        "unpriced": unpriced,
        "unscored": unscored,
        "no_starter_data": no_starter_data,
        "method_notes": [
            "Condition: Rest Edge (away B2B, home rested exactly 2 days) AND "
            "the away team started its own #1 goalie by cumulative starts "
            "entering that date, within that season only.",
            "Backup = starter with strictly fewer cumulative starts than the "
            f"team's leader entering that date, AND the team has played "
            f"{MIN_TEAM_GAMES}+ games so far this season. Games before that "
            "threshold are excluded from both buckets rather than guessed at.",
            "Schedule from historical_h2h_events_cache.json, pricing from "
            "historical_h2h_odds.json - identical sources and method to "
            "backtest_rest_signals.py.",
            "Starting goalies reconstructed fresh from NHL box scores for all "
            "four seasons (data/goalie_starts_backtest_cache.json), using the "
            "actual starter as a proxy for pre-game confirmation - the same "
            "method and caveat as backtest_signal2_history.py.",
            "ROI/CI method matches backtest_rest_signals.py: dollar-weighted "
            "ROI, 95% CI from the per-bet return distribution (mean +/- 1.96*SE).",
        ],
    }
    atomic_write_json(OUTPUT_PATH, output, indent=2)
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
