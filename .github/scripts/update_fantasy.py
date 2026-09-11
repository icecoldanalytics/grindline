#!/usr/bin/env python3
"""
Generates data/fantasy.json with AI-powered fantasy picks.
Fetches real rosters from NHL API to ensure accurate player/team data.
"""

import os
import json
import requests
from datetime import datetime
import pytz
import time
from generate_real_props import generate_real_player_props, append_to_props_log

MST = pytz.timezone("America/Edmonton")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def fetch_dashboard():
    try:
        with open("data/dashboard.json", "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Dashboard read error: {e}")
        return {}

def fetch_scratches():
    try:
        with open("data/scratches.json", "r") as f:
            data = json.load(f)
            scratched = data.get("scratched", [])
            print(f"Scratches loaded: {scratched}")
            return scratched
    except:
        return []

def fetch_rosters(games):
    rosters = {}
    for g in games:
        for team in [g["away"], g["home"]]:
            if team in rosters:
                continue
            try:
                url = f"https://api-web.nhle.com/v1/club-stats/{team}/now"
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                data = r.json()

                all_gp = [p.get("gamesPlayed", 0) for p in data.get("skaters", [])]
                avg_gp = sum(all_gp) / len(all_gp) if all_gp else 0
                min_gp = avg_gp * 0.4

                skaters = []
                for p in data.get("skaters", []):
                    fn = p.get("firstName", {}).get("default", "")
                    ln = p.get("lastName", {}).get("default", "")
                    pos = p.get("positionCode", "")
                    gp = p.get("gamesPlayed", 0)
                    pts = p.get("points", 0)
                    goals = p.get("goals", 0)
                    shots = p.get("shots", 0)
                    toi = round(p.get("avgTimeOnIcePerGame", 0) / 60, 1)
                    if gp < min_gp:
                        print(f"  Skipping likely injured: {fn} {ln} ({gp} GP vs {avg_gp:.0f} avg)")
                        continue
                    skaters.append(f"{fn} {ln} ({pos}, {gp}GP, {goals}G {pts}PTS, {shots}SOG, {toi}min TOI)")

                goalies = []
                for p in data.get("goalies", []):
                    fn = p.get("firstName", {}).get("default", "")
                    ln = p.get("lastName", {}).get("default", "")
                    gp = p.get("gamesPlayed", 0)
                    gs = p.get("gamesStarted", 0)
                    sv = round(p.get("savePercentage", 0), 3)
                    gaa = round(p.get("goalsAgainstAverage", 0), 2)
                    goalies.append(f"{fn} {ln} ({gp}GP, {gs}GS, .{str(sv)[2:]} SV%, {gaa} GAA)")

                rosters[team] = {"skaters": skaters[:20], "goalies": goalies}
                print(f"Roster fetched: {team} - {len(skaters)} active skaters, {len(goalies)} goalies")
                time.sleep(1)
            except Exception as e:
                print(f"Roster fetch error for {team}: {e}")
                rosters[team] = {"skaters": [], "goalies": []}
    return rosters

def call_claude(prompt):
    if not ANTHROPIC_API_KEY:
        print("No Anthropic API key")
        return None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        return json.loads(text.strip())
    except Exception as e:
        print(f"Claude API error: {e}")
        return None

def build_game_context(dashboard, rosters, scratches=[]):
    games = dashboard.get("games_tonight", [])
    if not games:
        return "No games tonight.", []
    lines = []
    for g in games:
        signal_note = ""
        if g["signal"] == "sig1":
            signal_note = f" [SIGNAL 1 - fade {g['away']}, home {g['home']} rested {g['home_rest']}+ days]"
        elif g["signal"] == "partial":
            signal_note = f" [SIGNAL 1 PARTIAL - fade {g['away']}, home rested 2 days]"
        elif g["signal"] == "cancel":
            signal_note = " [BOTH B2B - signals cancel]"
        odds_note = ""
        if g.get("away_ml") and g.get("home_ml"):
            odds_note = f" | ML: {g['away']} {g['away_ml']} / {g['home']} {g['home_ml']}"
        lines.append(f"- {g['away']} @ {g['home']} - {g['time_et']}{odds_note}{signal_note}")
        for team in [g["away"], g["home"]]:
            if team in rosters and rosters[team]["skaters"]:
                active_skaters = [s for s in rosters[team]["skaters"] if not any(sc.lower() in s.lower() for sc in scratches)]
                active_goalies = [s for s in rosters[team]["goalies"] if not any(sc.lower() in s.lower() for sc in scratches)]
                lines.append(f"  {team} skaters: {', '.join(active_skaters[:10])}")
                lines.append(f"  {team} goalies: {', '.join(active_goalies)}")
    return "\n".join(lines), games

def generate_value_plays(game_context, date_label, n_games):
    prompt = (
        "You are an expert NHL DFS and fantasy hockey analyst. Today is " + date_label + ".\n\n"
        "Tonight NHL slate with CONFIRMED CURRENT ROSTERS:\n"
        + game_context + "\n\n"
        "CRITICAL: Only use players listed above. Do not use players from your training data.\n"
        "Your training data is OUTDATED for current rosters, trades, and injuries.\n"
        "Only use players explicitly listed in the roster above.\n\n"
        "Generate fantasy value plays for both DraftKings and FanDuel. Apply signal logic where flagged.\n\n"
        "Respond ONLY with valid JSON, no markdown. Use this exact structure:\n"
        '{\n'
        '  "summary": {\n'
        '    "total_plays": 8,\n'
        '    "top_tier": "S",\n'
        f'    "slate_size": {n_games}\n'
        '  },\n'
        '  "plays": [\n'
        '    {\n'
        '      "player": "First Last",\n'
        '      "team": "ABBREV",\n'
        '      "position": "C",\n'
        '      "tier": "S",\n'
        '      "matchup": "vs OPP or @ OPP",\n'
        '      "game_time": "7:00 PM ET",\n'
        '      "dk_salary": "$8,400",\n'
        '      "fd_salary": "$7,200",\n'
        '      "proj_pts_dk": 21.4,\n'
        '      "proj_pts_fd": 38.2,\n'
        '      "dk_value": "2.55x",\n'
        '      "fd_value": "5.31x",\n'
        '      "reason": "2-3 sentence explanation with signal context where relevant",\n'
        '      "tags": ["DFS + Season"],\n'
        '      "format": "both"\n'
        '    }\n'
        '  ],\n'
        '  "avoids": [\n'
        '    {\n'
        '      "team": "ABBREV",\n'
        '      "reason": "Signal 1 fade - B2B away",\n'
        '      "tag": "Avoid DFS"\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        'Generate 6-10 plays across S/A/B tiers. Tags: "DFS + Season", "DFS Only", "Season-Long", "Signal 1 Game", "B2B Watch", "Avoid DFS".'
    )
    return call_claude(prompt)

def generate_goalie_starts(game_context, date_label, rosters, games):
    goalie_lines = []
    for g in games:
        away = g["away"]
        home = g["home"]
        signal_note = ""
        if g["signal"] == "sig1":
            signal_note = "SIGNAL 1 - home goalie boosted, away goalie downgraded"
        elif g["signal"] == "cancel":
            signal_note = "BOTH B2B - signals cancel"

        away_goalies = rosters.get(away, {}).get("goalies", ["Unknown"])
        home_goalies = rosters.get(home, {}).get("goalies", ["Unknown"])
        goalie_lines.append(f"- {away} @ {home} - {g['time_et']}{' | ' + signal_note if signal_note else ''}")
        goalie_lines.append(f"  {away} goalies: {', '.join(away_goalies)}")
        goalie_lines.append(f"  {home} goalies: {', '.join(home_goalies)}")

    goalie_context = "\n".join(goalie_lines)

    prompt = (
        "You are an expert NHL fantasy hockey analyst. Today is " + date_label + ".\n\n"
        "Games tonight with CONFIRMED CURRENT GOALIES:\n"
        + goalie_context + "\n\n"
        "CRITICAL: Only use goalies listed above. Your training data is outdated.\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{\n'
        '  "goalies": [\n'
        '    {\n'
        '      "name": "First Last",\n'
        '      "team": "ABBREV",\n'
        '      "opponent": "OPP",\n'
        '      "home_away": "home",\n'
        '      "dk_salary": "$8,200",\n'
        '      "fd_salary": "$9,000",\n'
        '      "sv_pct": ".921",\n'
        '      "gaa": "2.38",\n'
        '      "status": "confirmed",\n'
        '      "signal_note": "",\n'
        '      "recommendation": "start",\n'
        '      "rec_label": "Start"\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        'Status: "confirmed", "likely", "unknown", "b2b_away"\n'
        'Recommendation: "start", "stream", "wait", "avoid"\n'
        'Rec label: "Start", "Stream", "Wait", "Avoid"\n'
        'List one goalie per team. Hard avoid B2B away goalies.'
    )
    return call_claude(prompt)

def generate_player_props(game_context, date_label):
    prompt = (
        "You are an expert NHL prop betting analyst. Today is " + date_label + ".\n\n"
        "Tonight NHL slate with CONFIRMED CURRENT ROSTERS:\n"
        + game_context + "\n\n"
        "CRITICAL: Only use players listed above. Your training data is OUTDATED for current rosters, trades, and injuries.\n"
        "Do not use ANY player not explicitly listed in the rosters above.\n\n"
        "Generate player prop picks using ONLY players listed in the rosters above.\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{\n'
        '  "props": [\n'
        '    {\n'
        '      "player": "First Last",\n'
        '      "team": "ABBREV",\n'
        '      "prop_type": "Anytime Goal Scorer",\n'
        '      "line": "0.5",\n'
        '      "odds": "+135",\n'
        '      "pick": "over",\n'
        '      "unit_size": "full",\n'
        '      "game": "AWAY @ HOME",\n'
        '      "reason": "2 sentence explanation with signal context",\n'
        '      "category": "goals"\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        'Generate 8-12 props. Categories: "goals", "points", "shots", "assists"\n'
        'Pick: "over", "under", "back"\n'
        'Unit size: "full", "half", "avoid".'
    )
    return call_claude(prompt)

def main():
    now = datetime.now(MST)
    date_label = now.strftime("%A, %B %-d, %Y")
    today = now.strftime("%Y-%m-%d")

    print(f"Generating fantasy.json for {today}")

    dashboard = fetch_dashboard()
    games = dashboard.get("games_tonight", [])

    if not games:
        print("No games tonight - skipping fantasy generation")
        return

    print(f"Fetching rosters for {len(games)} games...")
    rosters = fetch_rosters(games)
    scratches = fetch_scratches()

    game_context, games_list = build_game_context(dashboard, rosters, scratches)

    print("Generating value plays...")
    value_plays = generate_value_plays(game_context, date_label, len(games))
    time.sleep(60)

    print("Generating goalie starts...")
    goalie_starts = generate_goalie_starts(game_context, date_label, rosters, games_list)
    time.sleep(60)

    print("Generating player props (real model vs real market odds)...")
    player_props = generate_real_player_props(games_list, scratches)

    if not value_plays or not goalie_starts or player_props is None:
        print("One or more sections failed - aborting")
        return

    output = {
        "date": today,
        "date_label": date_label,
        "value_plays": value_plays,
        "goalie_starts": goalie_starts,
        "player_props": player_props
    }

    os.makedirs("data", exist_ok=True)
    with open("data/fantasy.json", "w") as f:
        json.dump(output, f, indent=2)

    append_to_props_log(player_props.get("props", []), today)

    n_plays = len(value_plays.get("plays", []))
    n_goalies = len(goalie_starts.get("goalies", []))
    n_props = len(player_props.get("props", []))
    print(f"fantasy.json written - {n_plays} plays, {n_goalies} goalies, {n_props} props")

if __name__ == "__main__":
    main()
