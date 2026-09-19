#!/usr/bin/env python3
"""
Builds data/schedule_analysis.json: a full 2026-27 regular-season schedule
analysis for every current NHL team - back-to-back counts, games per
calendar week, and each team's heaviest/lightest week, plus a league-wide
weekly view. Serves two audiences: season-long players use games-per-week
for streaming and start/sit decisions; DFS players use back-to-back counts
and weekly slate size for nightly lineup building.

Data source: the NHL's own club-schedule-season endpoint - one free call
per team (32 calls total, no odds/API credits involved), filtered to
gameType == 2 (regular season only - this also drops preseason's
split-squad exhibition games, which would otherwise double-count some
dates). Confirmed against api.nhle.com/stats/rest/en/season on
2026-09-18: 2026-27 runs 2026-09-29 through 2027-04-10, 84 games/team.

Back-to-back: a team played the calendar day immediately before this game
- the same definition rest_edge.py and update_dashboard.py use elsewhere
in this repo.

Week: standard Monday-Sunday calendar weeks, keyed by that week's Monday
date. Not anchored to the season's own start date, since real fantasy
platforms almost universally use calendar weeks for matchup periods.

League-wide weekly totals are DISTINCT games (each game counted once, not
once per participating team) - deduplicated by the NHL's own game id,
since a plain sum of each team's weekly count would double every game.

Run:  python .github/scripts/build_schedule_analysis.py
Writes data/schedule_analysis.json. Safe to rerun anytime - the NHL
schedule is static once published except for rare in-season reschedules,
which a rerun picks up automatically.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import requests

# Windows' console defaults to cp1252, which can't encode the checkmark
# used in the summary print below; GitHub Actions' ubuntu runners default
# to UTF-8 already, so this only matters for local runs.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

SEASON = "20262027"
SEASON_LABEL = "2026-27"
SEASON_START = "2026-09-29"
SEASON_END = "2027-04-10"
OUTPUT_PATH = os.path.join("data", "schedule_analysis.json")

# Current 32 teams - copied from update_dashboard.py's FULL_NAMES (already
# excludes the retired ARI entry other, older scripts in this repo still carry).
FULL_NAMES = {
    "TOR": "Toronto Maple Leafs", "FLA": "Florida Panthers", "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres", "MTL": "Montréal Canadiens", "OTT": "Ottawa Senators",
    "DET": "Detroit Red Wings", "TBL": "Tampa Bay Lightning", "CAR": "Carolina Hurricanes",
    "NYR": "New York Rangers", "NYI": "New York Islanders", "NJD": "New Jersey Devils",
    "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins", "WSH": "Washington Capitals",
    "CBJ": "Columbus Blue Jackets", "CHI": "Chicago Blackhawks", "NSH": "Nashville Predators",
    "STL": "St. Louis Blues", "MIN": "Minnesota Wild", "WPG": "Winnipeg Jets",
    "COL": "Colorado Avalanche", "UTA": "Utah Mammoth", "CGY": "Calgary Flames",
    "EDM": "Edmonton Oilers", "VAN": "Vancouver Canucks", "SEA": "Seattle Kraken",
    "LAK": "Los Angeles Kings", "ANA": "Anaheim Ducks", "SJS": "San Jose Sharks",
    "VGK": "Vegas Golden Knights", "DAL": "Dallas Stars",
}


def atomic_write_json(path, data, indent=2):
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


def fetch_team_schedule(team):
    url = f"https://api-web.nhle.com/v1/club-schedule-season/{team}/{SEASON}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    games = [g for g in data.get("games", []) if g.get("gameType") == 2]
    games.sort(key=lambda g: g["gameDate"])
    return games


def week_start(date_str):
    """Monday of the calendar week containing date_str, as YYYY-MM-DD."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def analyze_team(team, games):
    dates = [g["gameDate"] for g in games]
    date_set = set(dates)

    b2b_dates = []
    for d in dates:
        prev = (datetime.strptime(d, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y-%m-%d")
        if prev in date_set:
            b2b_dates.append(d)

    weekly = {}
    for d in dates:
        wk = week_start(d)
        weekly[wk] = weekly.get(wk, 0) + 1
    weekly_list = [{"week_start": wk, "games": n} for wk, n in sorted(weekly.items())]

    heaviest = max(weekly_list, key=lambda w: w["games"])
    lightest = min(weekly_list, key=lambda w: w["games"])

    return {
        "team": team,
        "full_name": FULL_NAMES.get(team, team),
        "games": len(games),
        "back_to_backs": len(b2b_dates),
        "back_to_back_dates": b2b_dates,
        "weekly_games": weekly_list,
        "heaviest_week": heaviest,
        "lightest_week": lightest,
    }


def main():
    print(f"Building schedule analysis for {SEASON_LABEL}...")
    teams_out = {}
    all_games_by_id = {}  # dedup for the league-wide weekly view

    for team in sorted(FULL_NAMES):
        games = fetch_team_schedule(team)
        for g in games:
            all_games_by_id[g["id"]] = g["gameDate"]
        analysis = analyze_team(team, games)
        teams_out[team] = analysis
        print(f"  {team}: {analysis['games']} games, {analysis['back_to_backs']} B2Bs "
              f"(heaviest week {analysis['heaviest_week']['games']}g on {analysis['heaviest_week']['week_start']}, "
              f"lightest week {analysis['lightest_week']['games']}g on {analysis['lightest_week']['week_start']})")

    league_weekly = {}
    for date_str in all_games_by_id.values():
        wk = week_start(date_str)
        league_weekly[wk] = league_weekly.get(wk, 0) + 1
    league_weekly_list = [{"week_start": wk, "games": n} for wk, n in sorted(league_weekly.items())]
    heaviest_league_week = max(league_weekly_list, key=lambda w: w["games"])
    lightest_league_week = min(league_weekly_list, key=lambda w: w["games"])

    b2b_counts = {t: a["back_to_backs"] for t, a in teams_out.items()}
    most_b2b_team = max(b2b_counts, key=b2b_counts.get)
    fewest_b2b_team = min(b2b_counts, key=b2b_counts.get)

    output = {
        "season": SEASON_LABEL,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "season_start": SEASON_START,
        "season_end": SEASON_END,
        "total_games": len(all_games_by_id),
        "teams": teams_out,
        "league_weekly": league_weekly_list,
        "summary": {
            "heaviest_league_week": heaviest_league_week,
            "lightest_league_week": lightest_league_week,
            "most_back_to_backs": {"team": most_b2b_team, "count": b2b_counts[most_b2b_team]},
            "fewest_back_to_backs": {"team": fewest_b2b_team, "count": b2b_counts[fewest_b2b_team]},
            "avg_back_to_backs": round(sum(b2b_counts.values()) / len(b2b_counts), 1),
        },
    }

    os.makedirs("data", exist_ok=True)
    atomic_write_json(OUTPUT_PATH, output)

    print(f"\n{'='*60}")
    print(f"Total distinct games: {len(all_games_by_id)}")
    print(f"Most B2Bs:   {most_b2b_team} ({b2b_counts[most_b2b_team]})")
    print(f"Fewest B2Bs: {fewest_b2b_team} ({b2b_counts[fewest_b2b_team]})")
    print(f"Heaviest league week: {heaviest_league_week['week_start']} ({heaviest_league_week['games']} games)")
    print(f"Lightest league week: {lightest_league_week['week_start']} ({lightest_league_week['games']} games)")
    print(f"\n✓ Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
