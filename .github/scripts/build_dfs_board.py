#!/usr/bin/env python3
"""
Builds data/dfs_board.json: a DFS game-environment board for tonight's
NHL slate. Every number comes from the NHL API or The Odds API - nothing
is estimated, projected, or invented. Where real pregame confirmation
data does not exist (starting goalies), the board reports the real,
factual thing that does exist instead of guessing: which goalie started
that team's most recent game, plainly labeled "Last Start," never
"confirmed" or "projected."

Pricing: DraftKings h2h + totals, one book only - not averaged across
books. Pinnacle (the usual "sharp book" choice) is not available through
this project's Odds API plan at any region (verified against both live
and historical data, zero appearances), so mixing several recreational
books' totals and moneylines would produce a split no single book
actually prices. DraftKings was chosen because it had 100% coverage in a
500-game sample of this project's own historical odds data and is
already the book named in this site's existing DFS copy.

Implied team goals: each moneyline is de-vigged (raw implied probability
from both sides, normalized so they sum to 1), then the total is split
proportionally by those fair win probabilities:
    implied_goals(team) = total * fair_win_prob(team)
This is a mechanical derivation from two real market numbers, not a
model - the page states plainly that these are the market's own
expectation, not this site's projection.

Rest days: computed by the same day-by-day "who played on which date"
lookback update_dashboard.py and capture_signals.py use (not the
club-schedule-season endpoint), so preseason games count toward rest the
same way they do for the live Rest Edge signal - a team that played a
preseason game yesterday is on a back-to-back exactly as much as if it
were a regular-season game.

Goalie last-start and consecutive road games are scoped to REGULAR
SEASON games only (gameType == 2) within the current season - preseason
goalie usage isn't a reliable signal of regular-season role, and a road
trip spanning the off-season isn't meaningful.

Run:  python .github/scripts/build_dfs_board.py [YYYY-MM-DD]
Defaults to today. An explicit date lets this be tested against a real
past or future slate (e.g. the 2026-27 opener) without inventing data.
Writes data/dfs_board.json.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytz
import requests

MST = pytz.timezone("America/Edmonton")
SEASON = "20262027"
BOOK = "draftkings"
LOOKBACK_DAYS = 12
OUTPUT_PATH = os.path.join("data", "dfs_board.json")

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()

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

NAME_MAP = {  # abbrev -> lowercase city/name fragment, for Odds API team matching
    "tor": "toronto", "fla": "florida", "bos": "boston", "buf": "buffalo",
    "mtl": "montreal", "ott": "ottawa", "det": "detroit", "tbl": "tampa",
    "car": "carolina", "nyr": "new york rangers", "nyi": "new york islanders",
    "njd": "new jersey", "phi": "philadelphia", "pit": "pittsburgh",
    "wsh": "washington", "cbj": "columbus", "chi": "chicago",
    "nsh": "nashville", "stl": "st. louis", "min": "minnesota",
    "wpg": "winnipeg", "col": "colorado", "uta": "utah", "cgy": "calgary",
    "edm": "edmonton", "van": "vancouver", "sea": "seattle",
    "lak": "los angeles", "ana": "anaheim", "sjs": "san jose",
    "vgk": "vegas", "dal": "dallas",
}


def atomic_write_json(path, data, indent=2):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


# ── Schedule / rest ──────────────────────────────────────────────────────
def get_schedule(date_str):
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/schedule/{date_str}", timeout=15)
        r.raise_for_status()
        data = r.json()
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
    except Exception as e:
        print(f"Schedule error for {date_str}: {e}")
        return []


def get_teams_on_date(date_str):
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/schedule/{date_str}", timeout=15)
        r.raise_for_status()
        data = r.json()
        teams = set()
        for gw in data.get("gameWeek", []):
            if gw.get("date") != date_str:
                continue
            for g in gw.get("games", []):
                teams.add(g["awayTeam"]["abbrev"])
                teams.add(g["homeTeam"]["abbrev"])
        return teams
    except Exception:
        return set()


def rest_days_for(team, today, teams_by_date):
    """Exact integer days since this team's last game (any game type),
    same day-by-day lookback capture_signals.py uses - not capped at 3+."""
    for back in range(1, LOOKBACK_DAYS + 1):
        prev = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        if team in teams_by_date.get(prev, set()):
            return back
    return None


# ── Regular-season history (goalie last start + road streak) ────────────
def get_team_season_games(team):
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/club-schedule-season/{team}/{SEASON}", timeout=15)
        r.raise_for_status()
        data = r.json()
        games = [g for g in data.get("games", []) if g.get("gameType") == 2]
        games.sort(key=lambda g: g["gameDate"])
        return games
    except Exception as e:
        print(f"  season schedule error for {team}: {e}")
        return []


def road_streak(team, season_games, today_str):
    """Consecutive AWAY games ending with tonight (tonight counts as 1 if
    this team is the away side). Counts backward through completed games
    only; stops at the first home game or the start of the schedule."""
    past = [g for g in season_games if g["gameDate"] < today_str
            and g.get("gameState") in ("OFF", "FINAL")]
    streak = 1  # tonight itself
    for g in reversed(past):
        if g["awayTeam"]["abbrev"] == team:
            streak += 1
        else:
            break
    return streak


def get_starters(game_id):
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore", timeout=15)
        r.raise_for_status()
        box = r.json()
    except Exception as e:
        print(f"  boxscore error for game {game_id}: {e}")
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
    """This team's most recently completed regular-season game before
    today, and who started it. None if the team hasn't played yet this
    season - reported plainly on the page, never guessed at."""
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
def get_club_stats(team):
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/club-stats/{team}/now", timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  club-stats error for {team}: {e}")
        return {}


def goalie_stats_from_roster(roster, goalie_name):
    for g in roster.get("goalies", []):
        fn = g.get("firstName", {}).get("default", "")
        ln = g.get("lastName", {}).get("default", "")
        if f"{fn} {ln}" == goalie_name or ln == goalie_name.split()[-1]:
            sv = g.get("savePercentage")
            gaa = g.get("goalsAgainstAverage")
            return {
                "sv_pct": round(sv, 3) if sv is not None else None,
                "gaa": round(gaa, 2) if gaa is not None else None,
            }
    return {"sv_pct": None, "gaa": None}


def usage_leaders(roster, n=5):
    skaters = roster.get("skaters", [])
    rows = []
    for p in skaters:
        gp = p.get("gamesPlayed", 0)
        if gp <= 0:
            continue
        toi_sec = p.get("avgTimeOnIcePerGame", 0)
        rows.append({
            "player": f"{p.get('firstName', {}).get('default', '')} {p.get('lastName', {}).get('default', '')}".strip(),
            "position": p.get("positionCode", ""),
            "games_played": gp,
            "toi_per_game": round(toi_sec / 60, 1) if toi_sec else 0.0,
            "shots_per_game": round(p.get("shots", 0) / gp, 2),
        })
    rows.sort(key=lambda r: r["toi_per_game"], reverse=True)
    return rows[:n]


# ── Odds (DraftKings h2h + totals only) ──────────────────────────────────
def fetch_odds():
    if not ODDS_API_KEY:
        print("No ODDS_API_KEY - cannot price the board.")
        return []
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={
                "apiKey": ODDS_API_KEY, "regions": "us",
                "markets": "h2h,totals", "oddsFormat": "american",
                "bookmakers": BOOK,
            },
            timeout=20,
        )
        r.raise_for_status()
        print(f"  Odds credits remaining: {r.headers.get('x-requests-remaining', '?')}")
        return r.json()
    except Exception as e:
        print(f"Odds fetch error: {e}")
        return []


def find_event(events, away, home):
    a = NAME_MAP.get(away.lower(), away.lower())
    h = NAME_MAP.get(home.lower(), home.lower())
    for e in events:
        names = (e.get("home_team", "") + " " + e.get("away_team", "")).lower()
        if a in names and h in names:
            return e
    return None


def implied_prob(american):
    if american is None:
        return None
    return 100 / (american + 100) if american > 0 else abs(american) / (abs(american) + 100)


def price_game(event):
    """DraftKings h2h + totals for this event only. Returns None if
    DraftKings hasn't posted both markets for it yet."""
    if event is None:
        return None
    dk = next((b for b in event.get("bookmakers", []) if b.get("key") == BOOK), None)
    if dk is None:
        return None
    h2h = next((m for m in dk.get("markets", []) if m.get("key") == "h2h"), None)
    totals = next((m for m in dk.get("markets", []) if m.get("key") == "totals"), None)
    if h2h is None or totals is None:
        return None

    away_ml = home_ml = None
    for out in h2h.get("outcomes", []):
        if out["name"] == event["home_team"]:
            home_ml = out["price"]
        elif out["name"] == event["away_team"]:
            away_ml = out["price"]
    total = None
    for out in totals.get("outcomes", []):
        if out["name"] == "Over":
            total = out.get("point")
    if away_ml is None or home_ml is None or total is None:
        return None

    p_home_raw = implied_prob(home_ml)
    p_away_raw = implied_prob(away_ml)
    overround = p_home_raw + p_away_raw
    fair_home = p_home_raw / overround
    fair_away = p_away_raw / overround

    return {
        "book": BOOK, "total": total,
        "home_ml": home_ml, "away_ml": away_ml,
        "home_win_prob": round(fair_home, 3), "away_win_prob": round(fair_away, 3),
        "implied_home_goals": round(total * fair_home, 2),
        "implied_away_goals": round(total * fair_away, 2),
    }


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now(MST).strftime("%Y-%m-%d")
    d = datetime.strptime(date_str, "%Y-%m-%d")
    date_label = f"{d:%A, %B} {d.day}, {d.year}"
    print(f"Building DFS board for {date_str}...")

    games_today = get_schedule(date_str)
    if not games_today:
        print("  No games. Nothing to build.")
        atomic_write_json(OUTPUT_PATH, {
            "date": date_str, "date_label": date_label, "pricing_book": BOOK,
            "generated": datetime.now(MST).strftime("%Y-%m-%d %H:%M %p MT"),
            "games": [],
        })
        return

    playing_teams = sorted(set(g["away"] for g in games_today) | set(g["home"] for g in games_today))
    print(f"  {len(games_today)} games, {len(playing_teams)} teams.")

    # Rest days: day-by-day lookback (includes preseason - matches the live signal).
    d0 = datetime.strptime(date_str, "%Y-%m-%d").date()
    teams_by_date = {}
    for back in range(1, LOOKBACK_DAYS + 1):
        ds = (d0 - timedelta(days=back)).strftime("%Y-%m-%d")
        teams_by_date[ds] = get_teams_on_date(ds)

    print("  Fetching odds...")
    events = fetch_odds()

    print("  Fetching per-team season history + rosters...")
    season_games_by_team = {}
    roster_by_team = {}
    for team in playing_teams:
        season_games_by_team[team] = get_team_season_games(team)
        roster_by_team[team] = get_club_stats(team)

    games_out = []
    for g in games_today:
        away, home = g["away"], g["home"]
        event = find_event(events, away, home)
        pricing = price_game(event)

        away_rest = rest_days_for(away, d0, teams_by_date)
        home_rest = rest_days_for(home, d0, teams_by_date)

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

        games_out.append({
            "away": away, "home": home,
            "away_full": FULL_NAMES.get(away, away), "home_full": FULL_NAMES.get(home, home),
            "start_time_utc": g["startTimeUTC"],
            "pricing": pricing,
            "away_rest_days": away_rest, "home_rest_days": home_rest,
            "away_b2b": away_rest == 1, "home_b2b": home_rest == 1,
            "away_road_streak": road_streak(away, season_games_by_team[away], date_str),
            "home_road_streak": 0,
            "away_goalie": away_goalie, "home_goalie": home_goalie,
            "away_usage_leaders": usage_leaders(roster_by_team[away]),
            "home_usage_leaders": usage_leaders(roster_by_team[home]),
        })

    # Ranked by implied total (priced games first, then unpriced), highest first.
    games_out.sort(key=lambda g: (g["pricing"] is None, -(g["pricing"]["total"] if g["pricing"] else 0)))

    output = {
        "date": date_str, "date_label": date_label,
        "generated": datetime.now(MST).strftime("%Y-%m-%d %H:%M %p MT"),
        "pricing_book": BOOK,
        "pricing_note": "Implied totals and team goal splits are the market's own expectation "
                         "(DraftKings h2h + totals, de-vigged) - not a Grind Line projection.",
        "games": games_out,
    }
    atomic_write_json(OUTPUT_PATH, output)

    priced = sum(1 for g in games_out if g["pricing"])
    print(f"\n✓ {OUTPUT_PATH} written — {len(games_out)} games, {priced} priced by {BOOK}")
    for g in games_out:
        if g["pricing"]:
            print(f"  {g['away']} @ {g['home']}: total {g['pricing']['total']}  "
                  f"implied {g['pricing']['implied_away_goals']}-{g['pricing']['implied_home_goals']}")
        else:
            print(f"  {g['away']} @ {g['home']}: unpriced (DraftKings has no line yet)")


if __name__ == "__main__":
    main()
