#!/usr/bin/env python3
"""
Generates data/ticker.json with tonight's NHL games + best available odds.
Runs daily at 7 AM MST via GitHub Actions.
"""

import os
import json
import requests
from datetime import datetime
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

def fetch_schedule():
    today = datetime.now(MST).strftime("%Y-%m-%d")
    url = f"https://api-web.nhle.com/v1/schedule/{today}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        games = []
        for gw in data.get("gameWeek", []):
            if gw.get("date") == today:
                for g in gw.get("games", []):
                    try:
                        utc_time = datetime.strptime(g["startTimeUTC"], "%Y-%m-%dT%H:%M:%SZ")
                        utc_time = UTC.localize(utc_time)
                        mt_time = utc_time.astimezone(MST).strftime("%-I:%M %p MT")
                    except:
                        mt_time = "TBD"
                    games.append({
                        "away": g["awayTeam"]["abbrev"],
                        "home": g["homeTeam"]["abbrev"],
                        "time": mt_time
                    })
        print(f"Found {len(games)} games today")
        return games, today
    except Exception as e:
        print(f"Schedule error: {e}")
        return [], datetime.now(MST).strftime("%Y-%m-%d")

def fetch_odds():
    if not ODDS_API_KEY:
        print("No Odds API key — skipping odds")
        return []
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
                # Pinnacle isn't returned for NHL by this project's Odds API plan
                # (verified against both live and historical data, zero
                # appearances) - dropped rather than requested for nothing.
                "bookmakers": "draftkings,fanduel,betmgm"
            },
            timeout=10
        )
        r.raise_for_status()
        print(f"Odds API credits remaining: {r.headers.get('x-requests-remaining', 'unknown')}")
        return r.json()
    except Exception as e:
        print(f"Odds error: {e}")
        return []

def get_best_odds(away_abbr, home_abbr, odds_data):
    away_search = NAME_MAP.get(away_abbr.lower(), away_abbr.lower())
    home_search = NAME_MAP.get(home_abbr.lower(), home_abbr.lower())
    for event in odds_data:
        teams = [t.lower() for t in [event.get("home_team",""), event.get("away_team","")]]
        if any(away_search in t for t in teams) and any(home_search in t for t in teams):
            away_ml = None
            home_ml = None
            for bm in event.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market["key"] == "h2h":
                        for outcome in market.get("outcomes", []):
                            name = outcome["name"].lower()
                            price = outcome["price"]
                            if home_search in name and home_ml is None:
                                home_ml = price
                            elif away_search in name and away_ml is None:
                                away_ml = price
                if home_ml and away_ml:
                    break
            def fmt(o):
                if o is None: return None
                return f"+{o}" if o > 0 else str(o)
            return fmt(away_ml), fmt(home_ml)
    return None, None

def main():
    games, today = fetch_schedule()
    odds_data = fetch_odds()

    output_games = []
    for g in games:
        away_ml, home_ml = get_best_odds(g["away"], g["home"], odds_data)
        output_games.append({
            "away": g["away"],
            "home": g["home"],
            "time": g["time"],
            "away_ml": away_ml,
            "home_ml": home_ml
        })

    output = {
        "date": today,
        "games": output_games
    }

    os.makedirs("data", exist_ok=True)
    with open("data/ticker.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"✓ ticker.json written with {len(output_games)} games")

if __name__ == "__main__":
    main()
