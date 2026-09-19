#!/usr/bin/env python3
"""
Fetches live and final NHL scores and writes data/scores.json
Runs every 5 minutes during game hours via GitHub Actions.
"""

import os
import json
import sys
import requests
from datetime import datetime
import pytz

from team_names import CITY_NAMES as CITY_MAP

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

MST = pytz.timezone("America/Edmonton")

def main():
    today = datetime.now(MST).strftime("%Y-%m-%d")
    url = f"https://api-web.nhle.com/v1/score/{today}"
    
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Score fetch error: {e}")
        return

    games = []
    for g in data.get("games", []):
        state = g.get("gameState", "")
        away = g["awayTeam"]["abbrev"]
        home = g["homeTeam"]["abbrev"]
        away_score = g["awayTeam"].get("score", 0)
        home_score = g["homeTeam"].get("score", 0)

        period = g.get("periodDescriptor", {}).get("number", 0)
        period_type = g.get("periodDescriptor", {}).get("periodType", "REG")
        clock = g.get("clock", {}).get("timeRemaining", "")

        if period_type == "OT":
            period_label = "OT"
        elif period_type == "SO":
            period_label = "SO"
        elif period == 1:
            period_label = "1st"
        elif period == 2:
            period_label = "2nd"
        elif period == 3:
            period_label = "3rd"
        else:
            period_label = ""

        if state in ("LIVE", "CRIT"):
            status = "live"
            display = f"{period_label} · {clock}" if clock else period_label
        elif state in ("OFF", "FINAL"):
            status = "final"
            display = "Final"
            if period_type == "OT":
                display = "Final/OT"
            elif period_type == "SO":
                display = "Final/SO"
        elif state == "PRE":
            status = "pre"
            display = "Upcoming"
        else:
            status = "pre"
            display = "Upcoming"

        games.append({
            "away": away,
            "home": home,
            "away_city": CITY_MAP.get(away, away),
            "home_city": CITY_MAP.get(home, home),
            "away_score": away_score,
            "home_score": home_score,
            "status": status,
            "display": display,
            "period_label": period_label
        })

    output = {
        "date": today,
        "updated": datetime.now(MST).strftime("%I:%M %p MT"),
        "games": games
    }

    os.makedirs("data", exist_ok=True)
    with open("data/scores.json", "w") as f:
        json.dump(output, f, indent=2)

    live = sum(1 for g in games if g["status"] == "live")
    final = sum(1 for g in games if g["status"] == "final")
    print(f"✓ scores.json written — {live} live, {final} final, {len(games)} total")

if __name__ == "__main__":
    main()
