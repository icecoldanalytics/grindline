#!/usr/bin/env python3
"""
Generates data/dashboard.json with:
- Tonight's games + signal flags
- Last night's recap + signal results
- 2-day look-ahead
Runs daily at 7 AM MST via GitHub Actions.
"""

import os
import json
import requests
from datetime import datetime, timedelta
import pytz

MST = pytz.timezone("America/Edmonton")
UTC = pytz.utc
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

NAME_MAP = {
    "tor": "toronto", "fla": "florida", "bos": "boston", "buf": "buffalo",
    "mtl": "montreal", "ott": "ottawa", "det": "detroit", "tbl": "tampa",
    "car": "carolina", "nyr": "new york rangers", "nyi": "new york islanders",
    "njd": "new jersey", "phi": "philadelphia", "pit": "pittsburgh",
    "wsh": "washington", "cbj": "columbus", "chi": "chicago",
    "nsh": "nashville", "stl": "st. louis", "min": "minnesota",
    "wpg": "winnipeg", "col": "colorado", "uta": "utah", "cgy": "calgary",
    "edm": "edmonton", "van": "vancouver", "sea": "seattle",
    "lak": "los angeles", "ana": "anaheim", "sjs": "san jose",
    "vgk": "vegas", "dal": "dallas"
}

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
    "VGK": "Vegas Golden Knights", "DAL": "Dallas Stars"
}

CITY_NAMES = {
    "TOR": "Toronto", "FLA": "Florida", "BOS": "Boston", "BUF": "Buffalo",
    "MTL": "Montréal", "OTT": "Ottawa", "DET": "Detroit", "TBL": "Tampa Bay",
    "CAR": "Carolina", "NYR": "New York", "NYI": "New York", "NJD": "New Jersey",
    "PHI": "Philadelphia", "PIT": "Pittsburgh", "WSH": "Washington", "CBJ": "Columbus",
    "CHI": "Chicago", "NSH": "Nashville", "STL": "St. Louis", "MIN": "Minnesota",
    "WPG": "Winnipeg", "COL": "Colorado", "UTA": "Utah", "CGY": "Calgary",
    "EDM": "Edmonton", "VAN": "Vancouver", "SEA": "Seattle", "LAK": "Los Angeles",
    "ANA": "Anaheim", "SJS": "San Jose", "VGK": "Vegas", "DAL": "Dallas"
}

TEAM_NAMES = {
    "TOR": "Maple Leafs", "FLA": "Panthers", "BOS": "Bruins", "BUF": "Sabres",
    "MTL": "Canadiens", "OTT": "Senators", "DET": "Red Wings", "TBL": "Lightning",
    "CAR": "Hurricanes", "NYR": "Rangers", "NYI": "Islanders", "NJD": "Devils",
    "PHI": "Flyers", "PIT": "Penguins", "WSH": "Capitals", "CBJ": "Blue Jackets",
    "CHI": "Blackhawks", "NSH": "Predators", "STL": "Blues", "MIN": "Wild",
    "WPG": "Jets", "COL": "Avalanche", "UTA": "Mammoth", "CGY": "Flames",
    "EDM": "Oilers", "VAN": "Canucks", "SEA": "Kraken", "LAK": "Kings",
    "ANA": "Ducks", "SJS": "Sharks", "VGK": "Golden Knights", "DAL": "Stars"
}

def get_schedule(date_str):
    url = f"https://api-web.nhle.com/v1/schedule/{date_str}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        games = []
        for gw in data.get("gameWeek", []):
            if gw.get("date") == date_str:
                for g in gw.get("games", []):
                    try:
                        utc_time = datetime.strptime(g["startTimeUTC"], "%Y-%m-%dT%H:%M:%SZ")
                        utc_time = UTC.localize(utc_time)
                        mt_time = utc_time.astimezone(MST).strftime("%-I:%M %p MT")
                        et_time = utc_time.astimezone(pytz.timezone("America/New_York")).strftime("%-I:%M %p ET")
                    except:
                        mt_time = "TBD"
                        et_time = "TBD"
                    games.append({
                        "away": g["awayTeam"]["abbrev"],
                        "home": g["homeTeam"]["abbrev"],
                        "time_mt": mt_time,
                        "time_et": et_time,
                        "game_id": g.get("id", "")
                    })
        return games
    except Exception as e:
        print(f"Schedule error for {date_str}: {e}")
        return []

def get_scores(date_str):
    url = f"https://api-web.nhle.com/v1/score/{date_str}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        scores = []
        for g in data.get("games", []):
            if g.get("gameState") in ("OFF", "FINAL"):
                scores.append({
                    "away": g["awayTeam"]["abbrev"],
                    "home": g["homeTeam"]["abbrev"],
                    "away_score": g["awayTeam"].get("score", 0),
                    "home_score": g["homeTeam"].get("score", 0)
                })
        return scores
    except Exception as e:
        print(f"Scores error for {date_str}: {e}")
        return []

def get_teams_on_date(date_str):
    url = f"https://api-web.nhle.com/v1/schedule/{date_str}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        teams = set()
        for gw in data.get("gameWeek", []):
            if gw.get("date") == date_str:
                for g in gw.get("games", []):
                    teams.add(g["awayTeam"]["abbrev"])
                    teams.add(g["homeTeam"]["abbrev"])
        return teams
    except:
        return set()

def fetch_odds():
    if not ODDS_API_KEY:
        return []
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
                "bookmakers": "draftkings,fanduel,betmgm,pinnacle"
            },
            timeout=10
        )
        r.raise_for_status()
        print(f"Odds credits remaining: {r.headers.get('x-requests-remaining','?')}")
        return r.json()
    except Exception as e:
        print(f"Odds error: {e}")
        return []

def get_best_odds(away, home, odds_data):
    away_s = NAME_MAP.get(away.lower(), away.lower())
    home_s = NAME_MAP.get(home.lower(), home.lower())
    for event in odds_data:
        teams = [t.lower() for t in [event.get("home_team",""), event.get("away_team","")]]
        if any(away_s in t for t in teams) and any(home_s in t for t in teams):
            away_ml = home_ml = None
            for bm in event.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market["key"] == "h2h":
                        for outcome in market.get("outcomes", []):
                            n = outcome["name"].lower()
                            p = outcome["price"]
                            if home_s in n and home_ml is None:
                                home_ml = p
                            elif away_s in n and away_ml is None:
                                away_ml = p
                if home_ml and away_ml:
                    break
            def fmt(o):
                if o is None: return None
                return f"+{o}" if o > 0 else str(o)
            return fmt(away_ml), fmt(home_ml)
    return None, None

def get_rest_days(team, played_1_ago, played_2_ago):
    if team in played_1_ago: return 1
    if team in played_2_ago: return 2
    return 3

def detect_signal(away, home, b2b_teams, played_1_ago, played_2_ago):
    away_b2b = away in b2b_teams
    home_b2b = home in b2b_teams
    home_rest = get_rest_days(home, played_1_ago, played_2_ago)

    if away_b2b and home_b2b:
        return "cancel", away_b2b, home_b2b, home_rest
    if away_b2b and not home_b2b and home_rest >= 3:
        return "sig1", away_b2b, home_b2b, home_rest
    if away_b2b and not home_b2b and home_rest == 2:
        return "partial", away_b2b, home_b2b, home_rest
    return "none", away_b2b, home_b2b, home_rest

def main():
    now = datetime.now(MST)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    day_after = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    today_label = now.strftime("%B %-d, %Y")
    yesterday_label = (now - timedelta(days=1)).strftime("%B %-d")

    print(f"Generating dashboard.json for {today}")

    # Fetch schedules
    games_today = get_schedule(today)
    played_yesterday = get_teams_on_date(yesterday)
    played_two_days_ago = get_teams_on_date(two_days_ago)
    played_three_days_ago = get_teams_on_date(three_days_ago)

    # B2B = played yesterday AND playing today
    today_teams = set(g["away"] for g in games_today) | set(g["home"] for g in games_today)
    b2b_tonight = played_yesterday & today_teams

    # Fetch odds
    odds_data = fetch_odds()

    # ── TONIGHT'S GAMES ──
    games_tonight = []
    for g in games_today:
        away, home = g["away"], g["home"]
        signal, away_b2b, home_b2b, home_rest = detect_signal(
            away, home, b2b_tonight, played_yesterday, played_two_days_ago
        )
        away_ml, home_ml = get_best_odds(away, home, odds_data)
        games_tonight.append({
            "away": away,
            "home": home,
            "away_city": CITY_NAMES.get(away, away),
            "away_name": TEAM_NAMES.get(away, away),
            "home_city": CITY_NAMES.get(home, home),
            "home_name": TEAM_NAMES.get(home, home),
            "time_et": g["time_et"],
            "time_mt": g["time_mt"],
            "signal": signal,
            "away_b2b": away_b2b,
            "home_b2b": home_b2b,
            "home_rest": home_rest,
            "away_ml": away_ml,
            "home_ml": home_ml
        })

    # Sort: sig1 first, partial, cancel, none
    order = {"sig1": 0, "partial": 1, "cancel": 2, "none": 3}
    games_tonight.sort(key=lambda x: order.get(x["signal"], 3))

    # ── LAST NIGHT'S RECAP ──
    scores_yesterday = get_scores(yesterday)
    # For last night's signal check: b2b = played two_days_ago AND played yesterday
    yesterday_teams = get_teams_on_date(yesterday)
    b2b_yesterday = played_two_days_ago & yesterday_teams

    last_night = []
    for s in scores_yesterday:
        away, home = s["away"], s["home"]
        signal, away_b2b, home_b2b, home_rest = detect_signal(
            away, home, b2b_yesterday, played_two_days_ago, played_three_days_ago
        )
        home_won = s["home_score"] > s["away_score"]
        fade_won = home_won  # we always fade the away team

        result = "none"
        note = "No signal · Neither team on B2B"
        if signal == "sig1":
            result = "none"
            note = f"{away} on B2B away · {home} rested {home_rest}+ days — no pick (3+ days rest, retired)"
        elif signal == "partial":
            result = "hit" if fade_won else "miss"
            note = f"{away} on B2B away · {home} rested 2 days — Rest Edge, back {home}"
        elif signal == "cancel":
            result = "cancel"
            note = "Both teams on B2B — signal cancelled"

        last_night.append({
            "away": away,
            "home": home,
            "away_score": s["away_score"],
            "home_score": s["home_score"],
            "signal": signal,
            "result": result,
            "note": note,
            "fade_won": fade_won
        })

    # ── 2-DAY LOOK-AHEAD ──
    lookahead = []
    for date_str in [tomorrow, day_after]:
        date_label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %-d")
        games = get_schedule(date_str)
        # Teams on B2B for that day = played the day before
        day_before = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        played_day_before = get_teams_on_date(day_before)
        played_two_before = get_teams_on_date(
            (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
        )
        date_teams = set(g["away"] for g in games) | set(g["home"] for g in games)
        b2b_that_day = played_day_before & date_teams

        for g in games:
            away, home = g["away"], g["home"]
            signal, away_b2b, home_b2b, home_rest = detect_signal(
                away, home, b2b_that_day, played_day_before, played_two_before
            )
            if signal in ("sig1", "partial", "cancel"):
                lookahead.append({
                    "date": date_str,
                    "date_label": date_label,
                    "away": away,
                    "home": home,
                    "signal": signal,
                    "away_b2b": away_b2b,
                    "home_b2b": home_b2b,
                    "home_rest": home_rest,
                    "time_et": g["time_et"]
                })

    # ── SUMMARY STATS ──
    n_sig1 = sum(1 for g in games_tonight if g["signal"] == "sig1")
    n_partial = sum(1 for g in games_tonight if g["signal"] == "partial")

    output = {
        "date": today,
        "date_label": today_label,
        "yesterday_label": yesterday_label,
        "n_games": len(games_tonight),
        "n_sig1": n_sig1,
        "n_partial": n_partial,
        "games_tonight": games_tonight,
        "last_night": last_night,
        "lookahead": lookahead
    }

    os.makedirs("data", exist_ok=True)
    with open("data/dashboard.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"✓ dashboard.json written — {len(games_tonight)} games, {n_sig1} signals, {n_partial} partials")

if __name__ == "__main__":
    main()
