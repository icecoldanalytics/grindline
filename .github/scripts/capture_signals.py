#!/usr/bin/env python3
"""
Capture today's signal games with the moneyline available right now.

Scheduled to run each morning, but GitHub Actions' cron scheduling means
the actual run time varies day to day by hours, not minutes - confirmed
live across 15+ consecutive days. Never assume this happened at a fixed
time like 7 AM. What matters is that the exact capture time IS recorded,
honestly, per game: price_source is stamped "live_HH:MM_MT" using the
real clock time this specific run pulled odds, so update_roi.py (and the
site) can show each game's true capture time instead of a claimed one.

Appends ungraded entries to data/signal_log.json. update_roi.py fills in the
final scores on its next nightly run.

Costs 1 Odds API credit per run.
"""

import json
import os
from datetime import datetime, timedelta

import pytz
import requests

from rest_edge import home_ml_average, is_rest_edge

MST = pytz.timezone("America/Edmonton")
LOG_PATH = os.path.join("data", "signal_log.json")
LOOKBACK_DAYS = 10          # enough to establish rest for any team
MAX_LOOKBACK = 20

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()

NHL_TEAMS = {
    "ANA","ARI","BOS","BUF","CAR","CBJ","CGY","CHI","COL","DAL",
    "DET","EDM","FLA","LAK","MIN","MTL","NJD","NSH","NYI","NYR",
    "OTT","PHI","PIT","SEA","SJS","STL","TBL","TOR","UTA","VAN",
    "VGK","WPG","WSH",
}

NAME_KEY = {
    "ANA":"anaheim","ARI":"arizona","BOS":"boston","BUF":"buffalo",
    "CAR":"carolina","CBJ":"columbus","CGY":"calgary","CHI":"chicago",
    "COL":"colorado","DAL":"dallas","DET":"detroit","EDM":"edmonton",
    "FLA":"florida","LAK":"los angeles","MIN":"minnesota","MTL":"montr",
    "NJD":"new jersey","NSH":"nashville","NYI":"islanders","NYR":"rangers",
    "OTT":"ottawa","PHI":"philadelphia","PIT":"pittsburgh","SEA":"seattle",
    "SJS":"san jose","STL":"louis","TBL":"tampa","TOR":"toronto",
    "UTA":"utah","VAN":"vancouver","VGK":"vegas","WPG":"winnipeg",
    "WSH":"washington",
}


def teams_on(date_str):
    """Every team with a game scheduled or played on this date, of ANY
    game type. Deliberately unfiltered: rest-day counting includes
    preseason games (a team that played last night has less rest whether
    that game was preseason or not - see index.html's "Rest days include
    preseason games, matching how the live Rest Edge signal counts them"
    disclosure), and callers that need only regular-season games (i.e.
    which games are eligible to actually be LOGGED as a tracked signal
    occurrence) filter game_type themselves instead of being filtered
    here, so the rest-day history this feeds is never accidentally
    narrowed too."""
    try:
        r = requests.get(
            f"https://api-web.nhle.com/v1/score/{date_str}", timeout=15
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  NHL fetch failed for {date_str}: {e}")
        return set(), []

    teams, games = set(), []
    for g in data.get("games", []):
        away = g.get("awayTeam", {}).get("abbrev")
        home = g.get("homeTeam", {}).get("abbrev")
        if away not in NHL_TEAMS or home not in NHL_TEAMS:
            continue
        teams.add(away)
        teams.add(home)
        games.append({"away": away, "home": home, "game_type": g.get("gameType")})
    return teams, games


def rest_days(team, day, teams_by_date):
    for back in range(1, MAX_LOOKBACK + 1):
        prev = (day - timedelta(days=back)).strftime("%Y-%m-%d")
        if team in teams_by_date.get(prev, set()):
            return back
    return None


def fetch_live_odds():
    if not ODDS_API_KEY:
        print("  No ODDS_API_KEY — cannot log prices.")
        return []
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
            },
            timeout=20,
        )
        r.raise_for_status()
        print(f"  Odds credits remaining: "
              f"{r.headers.get('x-requests-remaining', '?')}")
        return r.json()
    except Exception as e:
        print(f"  Odds fetch failed: {e}")
        return []


def find_event(events, away, home):
    """Finds the event by fuzzy city-name matching (live odds use full
    team names; our signal candidates use abbreviations)."""
    a = NAME_KEY.get(away, away.lower())
    h = NAME_KEY.get(home, home.lower())
    for e in events:
        names = (e.get("home_team", "") + " " + e.get("away_team", "")).lower()
        if a not in names or h not in names:
            continue
        return e
    return None


def home_best_price(event):
    """Best (most favorable to a bettor) home price across bookmakers, for
    the home_ml_best field - the average itself comes from
    rest_edge.home_ml_average() using the event's exact home_team string."""
    prices = []
    home_team = event.get("home_team", "")
    for bk in event.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            if mkt.get("key") != "h2h":
                continue
            for out in mkt.get("outcomes", []):
                if out.get("name") == home_team:
                    prices.append(out["price"])
    if not prices:
        return None
    return max(prices, key=lambda p: (100 * 100 / abs(p)) if p < 0 else p)


def main():
    now = datetime.now(MST)
    today = now.date()
    today_str = today.strftime("%Y-%m-%d")
    print(f"Capturing signals for {today_str}...")

    # Build rest history
    teams_by_date = {}
    for back in range(1, LOOKBACK_DAYS + 1):
        ds = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        teams_by_date[ds], _ = teams_on(ds)

    _, todays_games_all = teams_on(today_str)
    if not todays_games_all:
        print("  No games today. Nothing to log.")
        return

    # Only regular-season games are eligible to be logged/graded as a
    # tracked signal occurrence - the published backtest (509 games,
    # 62.1%, +5.6% ROI) is regular-season only, so a preseason result
    # must never get blended into that same tracked record. Rest-day
    # counting above is deliberately unaffected by this - a preseason
    # game still counts as a team having played for rest purposes.
    todays_games = [g for g in todays_games_all if g["game_type"] == 2]
    skipped_preseason = [g for g in todays_games_all if g["game_type"] != 2]
    if skipped_preseason:
        print(f"  Excluding {len(skipped_preseason)} non-regular-season game(s) from logging: "
              + ", ".join(f"{g['away']}@{g['home']} (type {g['game_type']})" for g in skipped_preseason))
    if not todays_games:
        print("  No regular-season games today. Nothing to log.")
        return

    # Find signal games
    candidates = []
    for g in todays_games:
        away_rest = rest_days(g["away"], today, teams_by_date)
        home_rest = rest_days(g["home"], today, teams_by_date)
        if home_rest is None or not is_rest_edge(away_rest, home_rest):
            continue
        candidates.append(dict(g, away_rest=1, home_rest=home_rest))

    print(f"  {len(todays_games)} games, {len(candidates)} signal candidates.")
    if not candidates:
        return

    events = fetch_live_odds()

    # Load log and skip anything already recorded
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)
    else:
        log = {"schema": 1, "entries": []}

    seen = {(e["date"], e["away"], e["home"]) for e in log["entries"]}

    added = 0
    for c in candidates:
        key = (today_str, c["away"], c["home"])
        if key in seen:
            print(f"    already logged: {c['away']} @ {c['home']}")
            continue

        event = find_event(events, c["away"], c["home"])
        avg = home_ml_average(event.get("bookmakers", []), event.get("home_team", "")) if event else None
        if avg is None:
            print(f"    NO PRICE for {c['away']} @ {c['home']} — skipped")
            continue

        log["entries"].append({
            "date":         today_str,
            "away":         c["away"],
            "home":         c["home"],
            "away_rest":    1,
            "home_rest":    c["home_rest"],
            "signal":       "rest_edge",
            "home_ml_avg":  avg,
            "home_ml_best": home_best_price(event),
            "price_source": f"live_{now.strftime('%H:%M')}_MT",
            "graded":       False,
        })
        added += 1
        print(f"    logged {c['away']} @ {c['home']} "
              f"(home rest {c['home_rest']}d) at {avg:+.1f}")

    if added:
        log["entries"].sort(key=lambda e: (e["date"], e["away"], e["home"]))
        os.makedirs("data", exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
        print(f"\n✓ Added {added} entries to {LOG_PATH}")
    else:
        print("\n  Nothing new to add.")


if __name__ == "__main__":
    main()
