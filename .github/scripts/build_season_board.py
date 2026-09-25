#!/usr/bin/env python3
"""
Builds data/season_board.json: a season-long fantasy roster board - the
schedule-density and workload context that drives streaming and
start/sit decisions, not a nightly DFS board. Every number comes from
the NHL API or data/schedule_analysis.json (itself NHL-API-sourced) -
nothing is estimated, projected, or invented.

This replaced an earlier DFS-framed version (implied game totals, implied
team goals, stacking) - dropped entirely, not just de-emphasized, because
Alberta has no legal single-game DFS: that market-implied-total data
served a use case this audience doesn't have. What's left, and what this
script leads with, is squarely season-long: how many games a team plays
THIS calendar week (the core streaming decision - a 4-game week is worth
starting into, a 2-game week is a bye to plan around), rest days and
back-to-backs, which goalie last started for each team (a fact, not a
prediction - see the note below), and usage leaders by ice time and
shot rate.

Games-this-week comes straight from data/schedule_analysis.json
(build_schedule_analysis.py's weekly_games per team, Monday-Sunday
calendar weeks) - this script does not recompute it, just joins tonight's
teams against that already-published season-long view, and separately
surfaces a league-wide "who plays how much this week" ranking, since
that comparison across all 32 teams - not just tonight's - is the real
streaming signal.

Goalie last-start: the NHL API has no pregame starter-confirmation
endpoint (checked the gamecenter landing/matchup widget - it only has
season-level stat comparisons, not tonight's lineup), so this reports
the real factual thing that does exist instead of guessing: which
goalie started that team's most recent completed regular-season game,
and the date. Never labeled "confirmed" or "projected."

Rest days: the same day-by-day "who played on which date" lookback
update_dashboard.py and capture_signals.py use, restricted to
regular-season games (gameType == 2) only - matching exactly what
backtest_rest_signals.py's published record (509 games, 62.1%, +5.6%
ROI) validated, since its Odds-API-sourced data never contained a
preseason game to begin with.

Goalie SV%/GAA and skater usage leaders are joined against each team's
actual current roster (roster_stats.py), not read off club-stats/{team}/now
directly - that endpoint alone returns anyone with stats for a team,
including players since traded or waived off it (confirmed concretely:
Darnell Nurse, no longer on Edmonton, was showing up as a "current"
Edmonton usage leader before this fix). Every stat is tagged current vs.
last_season and the page must show that label - see roster_stats.py's
docstring.

Run:  python .github/scripts/build_season_board.py [YYYY-MM-DD]
Defaults to today. An explicit date lets this be tested against a real
past or future slate without inventing data. Writes data/season_board.json.
Requires data/schedule_analysis.json to already exist (build it first
with build_schedule_analysis.py).
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytz

from roster_stats import fetch_json_with_retry, roster_with_stats

MST = pytz.timezone("America/Edmonton")
SEASON = "20262027"
LOOKBACK_DAYS = 12
SCHEDULE_ANALYSIS_PATH = os.path.join("data", "schedule_analysis.json")
OUTPUT_PATH = os.path.join("data", "season_board.json")

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# Current 32 teams - same source as build_schedule_analysis.py's FULL_NAMES.
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
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


def week_start_of(date_obj):
    monday = date_obj - timedelta(days=date_obj.weekday())
    return monday.strftime("%Y-%m-%d")


def load_schedule_analysis():
    if not os.path.exists(SCHEDULE_ANALYSIS_PATH):
        raise SystemExit(
            f"{SCHEDULE_ANALYSIS_PATH} not found. Run build_schedule_analysis.py first - "
            f"this script joins against it rather than recomputing weekly game counts."
        )
    with open(SCHEDULE_ANALYSIS_PATH, encoding="utf-8") as f:
        return json.load(f)


def games_this_week(team, schedule_analysis, week_key):
    team_info = schedule_analysis.get("teams", {}).get(team)
    if not team_info:
        return None
    wk = next((w for w in team_info["weekly_games"] if w["week_start"] == week_key), None)
    return wk["games"] if wk else 0


# ── Schedule / rest (same day-by-day lookback as the live signal) ───────
def get_schedule(date_str):
    data = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/schedule/{date_str}", label=f"schedule/{date_str}"
    )
    if data is None:
        return []
    games = []
    for gw in data.get("gameWeek", []):
        if gw.get("date") != date_str:
            continue
        for g in gw.get("games", []):
            games.append({
                "away": g["awayTeam"]["abbrev"], "home": g["homeTeam"]["abbrev"],
                "startTimeUTC": g.get("startTimeUTC", ""),
            })
    return games


def get_teams_on_date(date_str):
    """Regular-season games only (gameType == 2) - see module docstring."""
    data = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/schedule/{date_str}", label=f"schedule/{date_str}"
    )
    if data is None:
        return set()
    teams = set()
    for gw in data.get("gameWeek", []):
        if gw.get("date") != date_str:
            continue
        for g in gw.get("games", []):
            if g.get("gameType") != 2:
                continue
            teams.add(g["awayTeam"]["abbrev"])
            teams.add(g["homeTeam"]["abbrev"])
    return teams


def rest_days_for(team, today, teams_by_date):
    for back in range(1, LOOKBACK_DAYS + 1):
        prev = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        if team in teams_by_date.get(prev, set()):
            return back
    return None


# ── Regular-season history (goalie last start) ───────────────────────────
def get_team_season_games(team):
    data = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/club-schedule-season/{team}/{SEASON}",
        label=f"season-schedule/{team}",
    )
    if data is None:
        return []
    games = [g for g in data.get("games", []) if g.get("gameType") == 2]
    games.sort(key=lambda g: g["gameDate"])
    return games


def get_starters(game_id):
    box = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore",
        label=f"boxscore/{game_id}",
    )
    if box is None:
        return "", ""

    pbs = box.get("playerByGameStats", {})

    def toi_seconds(s):
        try:
            m, sec = s.split(":")
            return int(m) * 60 + int(sec)
        except Exception:
            return 0

    result = {}
    for side in ("awayTeam", "homeTeam"):
        goalies = pbs.get(side, {}).get("goalies", [])
        starter = None
        for g in goalies:
            if g.get("starter") is True:
                starter = g
                break
        if starter is None and goalies:
            starter = max(goalies, key=lambda g: toi_seconds(g.get("toi", "0:00")))
        result[side] = starter.get("name", {}).get("default", "") if starter else ""
    return result.get("awayTeam", ""), result.get("homeTeam", "")


def last_start(team, season_games, today_str):
    past = [g for g in season_games if g["gameDate"] < today_str
            and g.get("gameState") in ("OFF", "FINAL")]
    if not past:
        return None
    game = past[-1]
    away_starter, home_starter = get_starters(game["id"])
    starter = away_starter if game["awayTeam"]["abbrev"] == team else home_starter
    if not starter:
        return None
    return {"goalie": starter, "date": game["gameDate"]}


# ── Roster (goalie SV%/GAA + skater usage leaders) ───────────────────────
# roster_stats.roster_with_stats() joins club-stats/{team}/now onto the
# team's actual current roster by playerId - see that module's docstring
# for the Darnell Nurse bug this replaced (club-stats alone includes
# anyone who has stats for a team, including players since traded away).

def goalie_stats_from_roster(team_roster, goalie_name):
    """team_roster is one team's roster_with_stats() output, or None if
    that team's roster fetch failed after retries - treated the same as
    an empty roster here (blank sv_pct/gaa/stat_season), never a crash."""
    if team_roster is None:
        return {"sv_pct": None, "gaa": None, "stat_season": None}
    for identity in team_roster.values():
        if identity["group"] != "goalies":
            continue
        full = f"{identity['first_name']} {identity['last_name']}"
        if full != goalie_name and identity["last_name"] != goalie_name.split()[-1]:
            continue
        s = identity["stats"]
        if s is None:
            return {"sv_pct": None, "gaa": None, "stat_season": None}
        sv = s.get("savePercentage")
        gaa = s.get("goalsAgainstAverage")
        return {
            "sv_pct": round(sv, 3) if sv is not None else None,
            "gaa": round(gaa, 2) if gaa is not None else None,
            "stat_season": identity["stat_season"],
        }
    return {"sv_pct": None, "gaa": None, "stat_season": None}


def usage_leaders(team_roster, n=5):
    """team_roster is one team's roster_with_stats() output, or None if
    that team's roster fetch failed after retries - returns [] in that
    case (blank, same as a team with no qualifying skaters) rather than
    crashing. Only players on the actual current roster are considered;
    a roster player with no stats entry (new signing, rookie) is simply
    not shown - never a substituted number."""
    if team_roster is None:
        return []
    rows = []
    for identity in team_roster.values():
        if identity["group"] == "goalies":
            continue
        s = identity["stats"]
        if s is None:
            continue
        gp = s.get("gamesPlayed", 0)
        if gp <= 0:
            continue
        toi_sec = s.get("avgTimeOnIcePerGame", 0)
        rows.append({
            "player": f"{identity['first_name']} {identity['last_name']}".strip(),
            "position": identity["position"],
            "games_played": gp,
            "toi_per_game": round(toi_sec / 60, 1) if toi_sec else 0.0,
            "shots_per_game": round(s.get("shots", 0) / gp, 2),
            "stat_season": identity["stat_season"],
        })
    rows.sort(key=lambda r: r["toi_per_game"], reverse=True)
    return rows[:n]


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(MST).strftime("%Y-%m-%d")
    d = datetime.strptime(date_str, "%Y-%m-%d")
    date_label = f"{d:%A, %B} {d.day}, {d.year}"
    week_key = week_start_of(d.date())
    print(f"Building season board for {date_str} (week of {week_key})...")

    schedule_analysis = load_schedule_analysis()

    # ── League-wide weekly workload: every team's games this week ──
    weekly_workload = []
    for team in sorted(FULL_NAMES):
        n = games_this_week(team, schedule_analysis, week_key)
        weekly_workload.append({"team": team, "full_name": FULL_NAMES[team], "games": n or 0})
    weekly_workload.sort(key=lambda t: (-t["games"], t["team"]))
    real_counts = [t["games"] for t in weekly_workload if t["games"] > 0]
    max_games = max(real_counts) if real_counts else 0
    min_games = min(real_counts) if real_counts else 0
    heavy_teams = [t["team"] for t in weekly_workload if t["games"] == max_games] if max_games else []
    light_teams = [t["team"] for t in weekly_workload if t["games"] == min_games] if min_games else []

    games_today = get_schedule(date_str)
    if not games_today:
        print("  No games tonight.")

    playing_teams = sorted(set(g["away"] for g in games_today) | set(g["home"] for g in games_today))

    d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    teams_by_date = {}
    for back in range(1, LOOKBACK_DAYS + 1):
        ds = (d0 - timedelta(days=back)).strftime("%Y-%m-%d")
        teams_by_date[ds] = get_teams_on_date(ds)

    print(f"  {len(games_today)} games tonight, {len(playing_teams)} teams. Fetching season history + rosters...")
    season_games_by_team = {}
    roster_by_team = {}
    failed_roster_teams = []
    for team in playing_teams:
        season_games_by_team[team] = get_team_season_games(team)
        roster = roster_with_stats(team, SEASON)
        if roster is None:
            failed_roster_teams.append(team)
        roster_by_team[team] = roster
        # Same reasoning as build_player_hub.py's per-team delay - this loop
        # had none before, and confirmed-live throttling elsewhere in this
        # repo's per-team fetches means it's not safe to assume this one is
        # exempt just because playing_teams is usually a small subset of 32.
        # Raised from 0.6s alongside that script's same bump - see its
        # comment for why (a follow-up run still saw scattered 429s there).
        time.sleep(1.5)

    if playing_teams and len(failed_roster_teams) / len(playing_teams) > 0.2:
        # Unlike a stable 32-team dataset, tonight's slate size varies
        # legitimately night to night, so there's no clean "previous file"
        # baseline to compare a total count against (see
        # build_player_hub.py/build_schedule_analysis.py for that version
        # of this guard). The equivalent guard here: if roster fetches
        # failed for more than a fifth of tonight's actual playing teams,
        # something is wrong with the API right now (not "these particular
        # teams have no players," which is never true) - abort and keep
        # the previous file rather than publish a board with usage
        # leaders and goalie stats missing for a chunk of tonight's games.
        print(f"\nABORTING: roster fetch failed after retries for {len(failed_roster_teams)} of "
              f"{len(playing_teams)} playing teams ({', '.join(failed_roster_teams)}) - over 20%. "
              "Keeping the previous file on disk instead of committing a board with holes in it.")
        sys.exit(1)
    elif failed_roster_teams:
        print(f"  WARNING: roster fetch failed after retries for {len(failed_roster_teams)} "
              f"team(s), proceeding with blank usage leaders/goalie stats for them: "
              f"{', '.join(failed_roster_teams)}")

    games_out = []
    for g in games_today:
        away, home = g["away"], g["home"]

        away_last = last_start(away, season_games_by_team[away], date_str)
        home_last = last_start(home, season_games_by_team[home], date_str)
        away_goalie = {"goalie": None, "date": None, "sv_pct": None, "gaa": None,
                        "note": "No games played yet this season"}
        if away_last:
            gs = goalie_stats_from_roster(roster_by_team[away], away_last["goalie"])
            away_goalie = {"goalie": away_last["goalie"], "date": away_last["date"], **gs}
        home_goalie = {"goalie": None, "date": None, "sv_pct": None, "gaa": None,
                        "note": "No games played yet this season"}
        if home_last:
            gs = goalie_stats_from_roster(roster_by_team[home], home_last["goalie"])
            home_goalie = {"goalie": home_last["goalie"], "date": home_last["date"], **gs}

        away_rest = rest_days_for(away, d0, teams_by_date)
        home_rest = rest_days_for(home, d0, teams_by_date)

        games_out.append({
            "away": away, "home": home,
            "away_full": FULL_NAMES.get(away, away), "home_full": FULL_NAMES.get(home, home),
            "start_time_utc": g["startTimeUTC"],
            "away_games_this_week": games_this_week(away, schedule_analysis, week_key),
            "home_games_this_week": games_this_week(home, schedule_analysis, week_key),
            "away_rest_days": away_rest, "home_rest_days": home_rest,
            "away_b2b": away_rest == 1, "home_b2b": home_rest == 1,
            "away_goalie": away_goalie, "home_goalie": home_goalie,
            "away_usage_leaders": usage_leaders(roster_by_team[away]),
            "home_usage_leaders": usage_leaders(roster_by_team[home]),
        })

    games_out.sort(key=lambda g: g["start_time_utc"])

    output = {
        "date": date_str, "date_label": date_label,
        "week_start": week_key,
        "generated": datetime.now(MST).strftime("%Y-%m-%d %H:%M %p MT"),
        "weekly_workload": {
            "teams": weekly_workload,
            "max_games": max_games, "min_games": min_games,
            "heavy_teams": heavy_teams, "light_teams": light_teams,
        },
        "games": games_out,
    }
    atomic_write_json(OUTPUT_PATH, output)

    print(f"\n✓ {OUTPUT_PATH} written — {len(games_out)} games")
    print(f"  This week ({week_key}): heaviest {max_games} games ({', '.join(heavy_teams) or 'n/a'}), "
          f"lightest {min_games} games ({', '.join(light_teams) or 'n/a'})")


if __name__ == "__main__":
    main()
