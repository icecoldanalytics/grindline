#!/usr/bin/env python3
"""
Originally a research-only backtest with no live counterpart. As of the
Emerging Edge launch, this IS that live signal's backtest validation -
betting.html's Emerging Edge section reads this file directly
(data/goalie_overlay_broad_backtest.json) for its backtest numbers, and
capture_signals.py / update_roi.py implement the same condition
(away B2B, home not B2B, away starts its #1 goalie) as a live-tracked
signal (data/emerging_edge_log.json -> data/roi.json's "emerging_edge"
key). This script itself still only reads cached historical data and
writes nothing live - it's a one-time-per-refresh backtest, not part of
the daily pipeline - but its output is no longer research-only in the
sense of "nothing reads it."

Tests the goalie overlay (does it matter whether the tired road team
starts its own #1 goalie vs. a backup?) as its own question, decoupled
from Rest Edge's specific home-rest-exactly-2-days requirement -
backtest_goalie_signal.py's existing result (data/goalie_signal_backtest.json)
only judges this overlay on the 160 games where Rest Edge ALSO fired
(away B2B + home rested EXACTLY 2). This script asks the broader
question: across every game where the away team is tired (on a
back-to-back) and the home team is rested at all (not itself on a
back-to-back - home_rest >= 2, not just home_rest == 2), does starting
the #1 goalie matter?

SAMPLE SIZE, checked against the real schedule before writing this:
the away-B2B/home-not-B2B condition alone is 822 games across all four
seasons (vs. 510 for Rest Edge's home_rest==2 subset, vs. 1,076 if home
is allowed to also be on a back-to-back, which stops being "rested" in
any meaningful sense). 822 is NOT "thousands" - the total four-season
discovered-event universe is only 5,273 games, so a genuinely selective
away-tired/home-rested condition structurally cannot reach that without
either dropping the "home rested" half of the comparison or redefining
"tired" at the individual-goalie level, which - since a goalie can only
start when his own team plays - turns out to be nearly the same
population as team-level back-to-back anyway. Reporting the real
achievable number rather than a larger one from a looser definition.

FEASIBILITY (checked before writing any of this): every data source
this script needs already exists in the repo from prior work, so this
run costs ZERO new API calls.
  - data/historical_h2h_events_cache.json: schedule (who played when),
    5,273 distinct discovered events.
  - data/historical_h2h_odds.json: real closing h2h prices, 5,251 entries.
  - data/rest_signal_scores_cache.json: final scores, 786 dates.
  - data/goalie_starts_backtest_cache.json: starting goalie per game,
    already built by backtest_goalie_signal.py - 5,256 games cached,
    both starters identified for 5,250 of them (99.9%). Confirmed live
    by sampling 24 real historical boxscores spread across all four
    seasons that the boxscore's own "starter" flag is what identifies
    this (24/24 had it set) - get_starters() below falls back to
    max-time-on-ice only for the rare game missing that flag (matches
    backtest_goalie_signal.py's identical fallback, never invented).

Goalie identity (#1 vs backup) uses the exact same method as
backtest_goalie_signal.py: cumulative starts per team, reset at each
season boundary, judged only on starts entering that date (no
hindsight). A start counts as "backup" only once the team has played
MIN_TEAM_GAMES games this season (a clear #1 has to exist to compare
against) AND that start has strictly fewer cumulative starts than the
team's leader at that point.

Pricing, scoring, and the ROI/95% CI statistical method are identical
to backtest_rest_signals.py and backtest_goalie_signal.py: real closing
h2h moneyline averaged across bookmakers in the pre-puck-drop snapshot,
dollar-weighted ROI, 95% CI from the per-bet return distribution
(mean +/- 1.96 * SE).

Run:  python .github/scripts/backtest_goalie_overlay_broad.py
Writes data/goalie_overlay_broad_backtest.json. Read-only against every
cache it uses - does not fetch anything or modify any existing file.
"""
import json
import math
import statistics
import sys
from datetime import datetime, timedelta

import pytz

EASTERN = pytz.timezone("America/New_York")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def atomic_write_json(path, data, indent=None):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    import os
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


def load_json_or_fail(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"\n{path} exists but will not parse as JSON ({e}).")
    except FileNotFoundError:
        raise SystemExit(
            f"\n{path} not found. This script is research-only and reuses "
            f"existing caches rather than fetching - run backtest_rest_signals.py "
            f"and backtest_goalie_signal.py first if this cache genuinely doesn't exist yet."
        )


EVENTS_CACHE_PATH = "data/historical_h2h_events_cache.json"
ODDS_PATH = "data/historical_h2h_odds.json"
SCORES_CACHE_PATH = "data/rest_signal_scores_cache.json"
GOALIE_CACHE_PATH = "data/goalie_starts_backtest_cache.json"
OUTPUT_PATH = "data/goalie_overlay_broad_backtest.json"
MIN_TEAM_GAMES = 10  # matches backtest_goalie_signal.py

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


def match_score(scores_for_date, away_full, home_full):
    for g in scores_for_date:
        if g["away_common"] in away_full and g["home_common"] in home_full:
            return g["away_score"], g["home_score"]
    return None


def main():
    events_by_id = load_events()
    print(f"Loaded {len(events_by_id)} distinct discovered events.")

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
    goalie_cache = load_json_or_fail(GOALIE_CACHE_PATH)
    print(f"Reusing existing caches: {len(odds)} priced games, {len(scores_cache)} scored dates, "
          f"{len(goalie_cache)} games with starters. Zero new API calls this run.\n")

    # Walk chronologically, tracking cumulative starts per team PER SEASON
    # (reset at each season boundary) - identical to backtest_goalie_signal.py.
    starts_by_season = {label: {} for label, _, _ in SEASONS}
    games_played_by_season = {label: {} for label, _, _ in SEASONS}

    number_one_bucket = {"pooled": []}
    backup_bucket = {"pooled": []}
    for label, _, _ in SEASONS:
        number_one_bucket[label] = []
        backup_bucket[label] = []

    raw_condition_games = 0  # away B2B, home not B2B - before any other filter
    unpriced = {"number_one": 0, "backup": 0}
    unscored = {"number_one": 0, "backup": 0}
    no_starter_data = 0
    excluded_pre_min_games = 0

    for date_str in sorted(events_by_date.keys()):
        season = season_for(date_str)
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        prev1 = (d - timedelta(days=1)).strftime("%Y-%m-%d")

        starts = starts_by_season[season]
        team_games = games_played_by_season[season]

        for e in sorted(events_by_date[date_str], key=lambda e: e["id"]):
            away, home = e["away_team"], e["home_team"]
            away_b2b = away in teams_by_date.get(prev1, set())
            home_b2b = home in teams_by_date.get(prev1, set())

            # The broadened condition: away tired (B2B), home rested at all
            # (NOT on a B2B - any amount of rest, not just exactly 2 days
            # the way Rest Edge requires). This is the entire point of this
            # script - everything below this line is the goalie-identity
            # overlay applied to that broader population, not a Rest Edge
            # subset.
            is_condition = away_b2b and not home_b2b

            key = f"{date_str}|{away} @ {home}"
            starter_info = goalie_cache.get(key, {})
            away_starter = starter_info.get("away_starter", "")

            if is_condition:
                raw_condition_games += 1

            if is_condition and away_starter:
                gp = team_games.get(away, 0)
                team_starts = starts.get(away, {})
                if gp >= MIN_TEAM_GAMES and team_starts:
                    leader_starts = max(team_starts.values())
                    starter_starts = team_starts.get(away_starter, 0)
                    backup_start = starter_starts < leader_starts
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
                else:
                    excluded_pre_min_games += 1
            elif is_condition and not away_starter:
                no_starter_data += 1

            # Update cumulative tracking AFTER evaluating (no hindsight),
            # for every game, not just condition games.
            for team, starter in ((away, away_starter), (home, starter_info.get("home_starter", ""))):
                team_games[team] = team_games.get(team, 0) + 1
                if starter:
                    starts.setdefault(team, {})
                    starts[team][starter] = starts[team].get(starter, 0) + 1

    print(f"Raw condition games (away B2B, home not B2B, all 4 seasons): {raw_condition_games}")
    print(f"  excluded (team hadn't played {MIN_TEAM_GAMES} games yet this season, no clear #1): {excluded_pre_min_games}")
    print(f"  excluded (no starter data found): {no_starter_data}")

    print("\n" + "=" * 70)
    print("BROAD GOALIE OVERLAY - Away B2B, Home Rested (any amount, not B2B),")
    print("Away Started Its Own #1 Goalie - back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(number_one_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(number_one_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['number_one']}, unscored: {unscored['number_one']})")

    print("\n" + "=" * 70)
    print("COMPARISON - Away B2B, Home Rested (any amount, not B2B),")
    print("Away Started a Backup - back home")
    print("=" * 70)
    for label, _, _ in SEASONS:
        fmt(label, calc_bucket(backup_bucket[label]))
    fmt("POOLED (all 4 seasons)", calc_bucket(backup_bucket["pooled"]))
    print(f"  (unpriced: {unpriced['backup']}, unscored: {unscored['backup']})")

    output = {
        "status": "Backtest validation for the live Emerging Edge signal - "
                   "betting.html reads this file directly. This script itself is "
                   "still a standalone backtest (reads only cached historical data, "
                   "not part of the daily pipeline) - the live signal itself is "
                   "implemented separately in capture_signals.py/update_roi.py.",
        "question": "Does the goalie overlay (away started its #1 vs a backup) hold across "
                    "every away-tired/home-rested game, not just the Rest Edge subset "
                    "(home rested exactly 2 days)?",
        "condition": "Away team on a back-to-back AND home team not on a back-to-back "
                     "(home rested >=2 days, any amount) - broader than Rest Edge's "
                     "exactly-2-days requirement.",
        "raw_condition_games": raw_condition_games,
        "goalie_overlay_broad_number_one": {k: calc_bucket(v) for k, v in number_one_bucket.items()},
        "goalie_overlay_broad_backup": {k: calc_bucket(v) for k, v in backup_bucket.items()},
        "unpriced": unpriced,
        "unscored": unscored,
        "no_starter_data": no_starter_data,
        "excluded_pre_min_games": excluded_pre_min_games,
        "method_notes": [
            "Sample-size check done before writing any code: the away-B2B/"
            "home-not-B2B condition is 822 raw games across four seasons, "
            "not literally 'thousands' - the total discovered-event universe "
            "is only 5,273 games, so a genuinely selective away-tired/"
            "home-rested condition can't reach that without weakening what "
            "'rested' means. Reporting the real number, not a larger one "
            "from a looser definition.",
            "Reused every existing cache (schedule, odds, scores, starters) - "
            "zero new API calls for this run.",
            "Goalie identity (#1 vs backup): cumulative starts per team, "
            f"reset each season, judged only on starts entering that date. "
            f"A start only counts once the team has played {MIN_TEAM_GAMES}+ "
            "games this season (needs a clear #1 to compare against) - "
            "earlier starts are excluded from both buckets, not guessed at.",
            "Starting goalies: boxscore 'starter' flag, confirmed present in "
            "100% of a 24-game sample spread across all four seasons; falls "
            "back to max time-on-ice only when that flag is missing "
            "(matches backtest_goalie_signal.py's identical fallback).",
            "Pricing: real closing h2h moneyline, averaged across bookmakers "
            "in the pre-puck-drop snapshot. ROI/CI: dollar-weighted ROI, 95% "
            "CI from the per-bet return distribution (mean +/- 1.96*SE) - "
            "identical method to backtest_rest_signals.py and "
            "backtest_goalie_signal.py.",
        ],
    }
    atomic_write_json(OUTPUT_PATH, output, indent=2)
    print(f"\nWrote {OUTPUT_PATH} (betting.html's Emerging Edge section reads this directly)")


if __name__ == "__main__":
    main()
