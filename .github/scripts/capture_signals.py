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

Appends ungraded entries to data/signal_log.json (Rest Edge) and
data/emerging_edge_log.json (Emerging Edge - away B2B, home rested ANY
amount, not just exactly 2 days). Same odds snapshot serves both -
Emerging Edge is a strict superset of Rest Edge's home_rest==2 condition,
so a game can legitimately be logged in both files; this does not cost a
second Odds API credit. update_roi.py fills in final scores on its next
nightly run, and - for Emerging Edge only - resolves which bucket
(started its #1 goalie vs. a backup) each game belongs to once that
game's boxscore exists; capture_signals.py runs before puck drop and has
no way to know who's actually starting, so away_starter/goalie_bucket
are logged null here and filled in later.

Costs 1 Odds API credit per run.
"""

import json
import os
from datetime import datetime, timedelta

import pytz
import requests

from rest_edge import home_ml_average, is_emerging_edge, is_rest_edge

MST = pytz.timezone("America/Edmonton")
LOG_PATH = os.path.join("data", "signal_log.json")
EMERGING_LOG_PATH = os.path.join("data", "emerging_edge_log.json")
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
    """Every team with a REGULAR-SEASON game (gameType == 2) scheduled or
    played on this date - used both for rest-day counting AND for which
    games are eligible to be logged. Confirmed live that these two must
    use the SAME definition: backtest_rest_signals.py's published record
    (509 games, 62.1%, +5.6% ROI) is regular-season-only not by choice but
    because its underlying Odds API discovery data never contained a
    single preseason game across all four backfilled seasons (earliest
    commence_time in data/historical_h2h_events_cache.json is 2022-10-07,
    the actual season opener) - so "preseason counts toward rest" was
    never what got validated, regardless of what this script or the site
    copy claimed. Restricting to regular season here means rest naturally
    can't be computed - and the signal naturally can't fire - during
    preseason at all, since there's no current-season regular-season
    history yet to look back through; no separate check is needed once
    the season is underway either, since by then every game in the
    lookback window is already regular season."""
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
        if g.get("gameType") != 2:
            continue
        away = g.get("awayTeam", {}).get("abbrev")
        home = g.get("homeTeam", {}).get("abbrev")
        if away not in NHL_TEAMS or home not in NHL_TEAMS:
            continue
        teams.add(away)
        teams.add(home)
        games.append({"away": away, "home": home})
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


def atomic_write_json(path, data, indent=2):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


def load_log(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"schema": 1, "entries": []}


def log_candidates(path, candidates, signal_name, today_str, now, events, extra_fields=None):
    """Shared append logic for both signal_log.json and
    emerging_edge_log.json - same skip-if-already-logged, same price
    lookup against the one shared odds snapshot, same atomic write."""
    log = load_log(path)
    seen = {(e["date"], e["away"], e["home"]) for e in log["entries"]}

    added = 0
    for c in candidates:
        key = (today_str, c["away"], c["home"])
        if key in seen:
            print(f"    [{signal_name}] already logged: {c['away']} @ {c['home']}")
            continue

        event = find_event(events, c["away"], c["home"])
        avg = home_ml_average(event.get("bookmakers", []), event.get("home_team", "")) if event else None
        if avg is None:
            print(f"    [{signal_name}] NO PRICE for {c['away']} @ {c['home']} — skipped")
            continue

        entry = {
            "date":         today_str,
            "away":         c["away"],
            "home":         c["home"],
            "away_rest":    1,
            "home_rest":    c["home_rest"],
            "signal":       signal_name,
            "home_ml_avg":  avg,
            "home_ml_best": home_best_price(event),
            "price_source": f"live_{now.strftime('%H:%M')}_MT",
            "graded":       False,
        }
        if extra_fields:
            entry.update(extra_fields)
        log["entries"].append(entry)
        added += 1
        print(f"    [{signal_name}] logged {c['away']} @ {c['home']} "
              f"(home rest {c['home_rest']}d) at {avg:+.1f}")

    if added:
        log["entries"].sort(key=lambda e: (e["date"], e["away"], e["home"]))
        os.makedirs("data", exist_ok=True)
        atomic_write_json(path, log)
        print(f"  ✓ Added {added} entries to {path}")
    else:
        print(f"  [{signal_name}] nothing new to add.")
    return added


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

    _, todays_games = teams_on(today_str)
    if not todays_games:
        print("  No regular-season games today. Nothing to log.")
        return

    # Find candidates for both signals from the same rest computation.
    # Emerging Edge is a superset of Rest Edge (home_rest==2 satisfies
    # both) - a game can legitimately land in both candidate lists.
    rest_edge_candidates = []
    emerging_candidates = []
    for g in todays_games:
        away_rest = rest_days(g["away"], today, teams_by_date)
        home_rest = rest_days(g["home"], today, teams_by_date)
        if home_rest is None:
            continue
        if is_rest_edge(away_rest, home_rest):
            rest_edge_candidates.append(dict(g, away_rest=1, home_rest=home_rest))
        if is_emerging_edge(away_rest, home_rest):
            emerging_candidates.append(dict(g, away_rest=1, home_rest=home_rest))

    print(f"  {len(todays_games)} games, {len(rest_edge_candidates)} Rest Edge candidates, "
          f"{len(emerging_candidates)} Emerging Edge candidates.")
    if not rest_edge_candidates and not emerging_candidates:
        return

    events = fetch_live_odds()

    log_candidates(LOG_PATH, rest_edge_candidates, "rest_edge", today_str, now, events)

    # Emerging Edge: away_starter/goalie_bucket start null - capture runs
    # before puck drop, so who's actually starting isn't knowable yet.
    # update_roi.py resolves these once each game's boxscore exists.
    log_candidates(
        EMERGING_LOG_PATH, emerging_candidates, "emerging_edge", today_str, now, events,
        extra_fields={"away_starter": None, "goalie_bucket": None},
    )


if __name__ == "__main__":
    main()
