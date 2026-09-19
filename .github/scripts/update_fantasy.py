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
import unicodedata

from roster_stats import roster_with_stats

MST = pytz.timezone("America/Edmonton")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PROP_LOG_PATH = "data/prop_log.json"
SEASON = "20262027"

PROP_MARKETS = [
    "player_goal_scorer_anytime",
    "player_shots_on_goal",
    "player_points",
    "player_assists",
]

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
    """Real current-roster players only, joined against club-stats/{team}/now
    by playerId via roster_stats.py - NOT club-stats read directly, which
    returns anyone with stats for a team including players since traded or
    waived off it. Confirmed concretely: Darnell Nurse (no longer on
    Edmonton) was appearing here and being fed to the AI as a real,
    current Oilers roster player. See roster_stats.py's docstring.

    A roster player with no stats entry (new signing, rookie - nothing to
    ground a pick in) is left out of the context entirely rather than
    included with invented numbers. Every included player's stat line is
    labeled "last season" whenever that's genuinely what it is (the
    current season hasn't started, or this club-stats snapshot predates
    it) - never presented to the model as current form.
    """
    rosters = {}
    for g in games:
        for team in [g["away"], g["home"]]:
            if team in rosters:
                continue
            try:
                team_roster = roster_with_stats(team, SEASON)

                skater_gp = [v["stats"].get("gamesPlayed", 0) for v in team_roster.values()
                             if v["group"] != "goalies" and v["stats"]]
                avg_gp = sum(skater_gp) / len(skater_gp) if skater_gp else 0
                min_gp = avg_gp * 0.4

                skaters = []
                for v in team_roster.values():
                    if v["group"] == "goalies":
                        continue
                    s = v["stats"]
                    if s is None:
                        continue  # on roster, no stats to ground a pick in - not invented
                    fn, ln, pos = v["first_name"], v["last_name"], v["position"]
                    gp = s.get("gamesPlayed", 0)
                    pts = s.get("points", 0)
                    goals = s.get("goals", 0)
                    shots = s.get("shots", 0)
                    toi = round(s.get("avgTimeOnIcePerGame", 0) / 60, 1)
                    if gp < min_gp:
                        print(f"  Skipping likely injured: {fn} {ln} ({gp} GP vs {avg_gp:.0f} avg)")
                        continue
                    season_note = " — last season" if v["stat_season"] == "last_season" else ""
                    skaters.append(f"{fn} {ln} ({pos}, {gp}GP, {goals}G {pts}PTS, {shots}SOG, {toi}min TOI{season_note})")

                goalies = []
                for v in team_roster.values():
                    if v["group"] != "goalies":
                        continue
                    s = v["stats"]
                    if s is None:
                        continue
                    fn, ln = v["first_name"], v["last_name"]
                    gp = s.get("gamesPlayed", 0)
                    gs = s.get("gamesStarted", 0)
                    sv = round(s.get("savePercentage", 0), 3)
                    gaa = round(s.get("goalsAgainstAverage", 0), 2)
                    season_note = " — last season" if v["stat_season"] == "last_season" else ""
                    goalies.append(f"{fn} {ln} ({gp}GP, {gs}GS, .{str(sv)[2:]} SV%, {gaa} GAA{season_note})")

                rosters[team] = {"skaters": skaters[:20], "goalies": goalies}
                print(f"Roster fetched: {team} - {len(skaters)} active skaters, {len(goalies)} goalies")
                time.sleep(1)
            except Exception as e:
                print(f"Roster fetch error for {team}: {e}")
                rosters[team] = {"skaters": [], "goalies": []}
    return rosters

def normalize_name(name):
    """Fold a player name to a comparable key.

    Accents are stripped (Barre-Boulet == Barré-Boulet) but hyphens and
    apostrophes are kept, since they distinguish real names. Curly quotes are
    folded to straight ones because model output and the NHL API disagree there.
    """
    if not name:
        return ""
    name = str(name).replace("’", "'").replace("ʼ", "'")
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())

def parse_roster_name(entry):
    """Pull the name off a roster string built by fetch_rosters().

    Entries look like "Connor McDavid (C, 82GP, 48G 138PTS, 306SOG, 23.0min TOI)"
    for skaters and "Stuart Skinner (23GP, 23GS, .891 SV%, 2.83 GAA)" for
    goalies, so the name is everything before the first " (".
    """
    return entry.split(" (", 1)[0].strip()

def build_roster_name_set(rosters):
    """Every skater and goalie currently on a fetched roster, normalized."""
    names = set()
    for team in rosters.values():
        for entry in team.get("skaters", []) + team.get("goalies", []):
            normalized = normalize_name(parse_roster_name(entry))
            if normalized:
                names.add(normalized)
    return names

def validate_players(section, key, valid_names, label):
    """Drop entries naming a player who is not in valid_names.

    The model is told to use only the listed players, but it can still fall back
    on training data. Anything it invents gets removed here rather than shipped.
    """
    if not section or not valid_names:
        return section
    entries = section.get(key)
    if not isinstance(entries, list):
        return section

    kept, dropped = [], []
    for entry in entries:
        if not isinstance(entry, dict):
            dropped.append(str(entry))
            continue
        name = entry.get("player") or entry.get("name") or ""
        if normalize_name(name) in valid_names:
            kept.append(entry)
        else:
            dropped.append(name or str(entry))

    if dropped:
        print(f"{label}: dropped {len(dropped)} of {len(entries)} - not on any fetched roster:")
        for name in dropped:
            print(f"  - {name}")
    else:
        print(f"{label}: all {len(entries)} entries verified against rosters")

    section[key] = kept
    return section

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
                "max_tokens": 8000,
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
        if g["signal"] == "rest_edge":
            signal_note = f" [{g['away']} on road B2B; {g['home']} rested 2 days]"
        elif g["signal"] == "cancel":
            signal_note = " [Both teams on B2B]"
        odds_note = ""
        if g.get("away_ml") and g.get("home_ml"):
            odds_note = f" | ML: {g['away']} {g['away_ml']} / {g['home']} {g['home_ml']}"
        lines.append(f"- {g['away']} @ {g['home']} - {g['time_et']}{odds_note}{signal_note}")
        scratch_keys = {normalize_name(sc) for sc in scratches if normalize_name(sc)}
        for team in [g["away"], g["home"]]:
            if team in rosters and rosters[team]["skaters"]:
                # Match on the parsed name only - matching the whole roster
                # string would also test a scratch against the stat block.
                active_skaters = [s for s in rosters[team]["skaters"] if normalize_name(parse_roster_name(s)) not in scratch_keys]
                active_goalies = [s for s in rosters[team]["goalies"] if normalize_name(parse_roster_name(s)) not in scratch_keys]
                lines.append(f"  {team} skaters: {', '.join(active_skaters[:10])}")
                lines.append(f"  {team} goalies: {', '.join(active_goalies)}")
    return "\n".join(lines), games

def generate_value_plays(game_context, date_label, n_games):
    prompt = (
        "You are an expert NHL fantasy hockey analyst. Today is " + date_label + ".\n\n"
        "Tonight's NHL slate with CONFIRMED CURRENT ROSTERS:\n"
        + game_context + "\n\n"
        "CRITICAL: Only use players listed above. Do not use players from your training data.\n"
        "Your training data is OUTDATED for current rosters, trades, and injuries.\n"
        "Only use players explicitly listed in the roster above.\n\n"
        "Do NOT output salaries, prices, projected point totals, or value multiples.\n"
        "You have no access to DraftKings or FanDuel salary data. Never invent a number.\n"
        "For usage_note, copy the TOI and shot figures verbatim from that player's roster\n"
        "line above. Do not estimate, round, or adjust them.\n\n"
        "Generate plays useful to both daily and season-long players. Ground every pick in\n"
        "rest, schedule spot, matchup and usage.\n\n"
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
        '      "usage_note": "18.4min TOI, 3.1 SOG",\n'
        '      "reason": "2-3 sentences grounded in rest, matchup and usage",\n'
        '      "tags": ["Season-Long"],\n'
        '      "audience": "both"\n'
        '    }\n'
        '  ],\n'
        '  "avoids": [\n'
        '    {\n'
        '      "team": "ABBREV",\n'
        '      "reason": "Second half of a road back-to-back",\n'
        '      "tag": "Avoid"\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        'Generate 6-10 plays across S/A/B tiers.\n'
        'Tags: "DFS Spot", "Season-Long", "Streamer", "B2B Watch", "Rest Advantage".\n'
        'Audience: "dfs", "season", "both".'
    )
    return call_claude(prompt)

def generate_goalie_starts(game_context, date_label, rosters, games):
    goalie_lines = []
    for g in games:
        away = g["away"]
        home = g["home"]
        rest_note = ""
        if g["signal"] == "rest_edge":
            rest_note = f"{away} on road B2B; {home} rested 2 days"
        elif g["signal"] == "cancel":
            rest_note = "Both teams on B2B"

        away_goalies = rosters.get(away, {}).get("goalies", ["Unknown"])
        home_goalies = rosters.get(home, {}).get("goalies", ["Unknown"])
        goalie_lines.append(f"- {away} @ {home} - {g['time_et']}{' | ' + rest_note if rest_note else ''}")
        goalie_lines.append(f"  {away} goalies: {', '.join(away_goalies)}")
        goalie_lines.append(f"  {home} goalies: {', '.join(home_goalies)}")

    goalie_context = "\n".join(goalie_lines)

    prompt = (
        "You are an expert NHL fantasy hockey analyst. Today is " + date_label + ".\n\n"
        "Games tonight with CONFIRMED CURRENT GOALIES:\n"
        + goalie_context + "\n\n"
        "CRITICAL: Only use goalies listed above. Your training data is outdated.\n"
        "Do NOT output salaries or prices. You have no access to salary data.\n"
        "Copy sv_pct and gaa verbatim from the goalie line above. Never invent a number.\n"
        "If a goalie's stats are not shown above, use null for sv_pct and gaa.\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{\n'
        '  "goalies": [\n'
        '    {\n'
        '      "name": "First Last",\n'
        '      "team": "ABBREV",\n'
        '      "opponent": "OPP",\n'
        '      "home_away": "home",\n'
        '      "sv_pct": ".921",\n'
        '      "gaa": "2.38",\n'
        '      "status": "confirmed",\n'
        '      "rest_note": "",\n'
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
    
def fetch_events():
    """Today's NHL event IDs from The Odds API. This endpoint is free."""
    url = "https://api.the-odds-api.com/v4/sports/icehockey_nhl/events"
    try:
        r = requests.get(url, params={"apiKey": ODDS_API_KEY}, timeout=15)
        r.raise_for_status()
        events = r.json()
        print(f"Events found: {len(events)}")
        return events
    except Exception as e:
        print(f"Events fetch error: {e}")
        return []


def fetch_player_props(events):
    """One call per market per event so a bad key can't kill the batch."""
    if not ODDS_API_KEY:
        print("No Odds API key - skipping props")
        return []
    props = []
    for ev in events:
        for market in PROP_MARKETS:
            url = f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/events/{ev['id']}/odds"
            try:
                r = requests.get(url, params={
                    "apiKey": ODDS_API_KEY,
                    "regions": "us",
                    "markets": market,
                    "oddsFormat": "american",
                }, timeout=15)
                if r.status_code in (400, 404, 422):
                    print(f"  {market} unavailable for {ev.get('away_team')} @ {ev.get('home_team')} ({r.status_code})")
                    continue
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                print(f"  Prop fetch error [{market}]: {e}")
                continue
            for bm in data.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    for o in mk.get("outcomes", []):
                        if not o.get("description") or o.get("price") is None:
                            continue
                        props.append({
                            "player": o["description"],
                            "market": mk.get("key", market),
                            "book": bm.get("key", ""),
                            "side": o.get("name", ""),
                            "line": o.get("point"),
                            "price": o["price"],
                            "game": f"{ev.get('away_team')} @ {ev.get('home_team')}",
                        })
            time.sleep(0.5)
    print(f"Prop outcomes fetched: {len(props)}")
    return props


def build_prop_context(props, per_market=15):
    """Best available price per player/market/side, capped so the prompt stays sane."""
    if not props:
        return ""
    best = {}
    for p in props:
        key = (p["player"], p["market"], p["side"], p["line"])
        if key not in best or p["price"] > best[key]["price"]:
            best[key] = p
    by_market = {}
    for p in best.values():
        by_market.setdefault(p["market"], []).append(p)
    lines = []
    for market, rows in sorted(by_market.items()):
        rows.sort(key=lambda x: x["player"])
        lines.append(f"{market}:")
        for p in rows[:per_market]:
            line_str = f" {p['line']}" if p["line"] is not None else ""
            price = f"+{p['price']}" if p["price"] > 0 else str(p["price"])
            lines.append(f"  - {p['player']} | {p['side']}{line_str} @ {price} ({p['book']}) | {p['game']}")
    return "\n".join(lines)
def generate_player_props(prop_context, date_label):
    if not prop_context:
        print("No prop lines available - skipping props section")
        return {"props": [], "note": "No prop markets posted yet for tonight's slate."}

    prompt = (
        "You are an expert NHL prop analyst. Today is " + date_label + ".\n\n"
        "REAL prop lines currently posted, best available price per player:\n"
        + prop_context + "\n\n"
        "Select the most attractive props from the list above.\n"
        "CRITICAL: use ONLY players, lines, prices and books shown above.\n"
        "Copy the line, odds and book verbatim. Never invent or adjust a number.\n"
        "If you cannot justify a pick from the data shown, return fewer picks.\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        '{\n'
        '  "props": [\n'
        '    {\n'
        '      "player": "First Last",\n'
        '      "market": "player_shots_on_goal",\n'
        '      "line": "2.5",\n'
        '      "side": "Over",\n'
        '      "odds": "+135",\n'
        '      "book": "draftkings",\n'
        '      "game": "AWAY @ HOME",\n'
        '      "reason": "2 sentences grounded in usage, matchup or rest",\n'
        '      "confidence": "medium"\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        'Return 5-10 props. Confidence: "high", "medium", "low".'
    )
    return call_claude(prompt)


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


def log_props(props, date_str):
    """Appends today's real, validated props to data/prop_log.json as
    ungraded entries - the same append_to_props_log() call this replaced
    (imported from generate_real_props.py) required category/prop_type/
    pick/unit_size, fields that belong to generate_real_props.py's
    model-based prop schema, not this file's own generate_player_props()
    output (market/line/side/odds/book/confidence) - it would KeyError
    the moment any real props existed to log. This logs the schema this
    pipeline actually produces, following capture_signals.py's pattern:
    resumable (skips anything already logged today), atomic write,
    graded later by a separate script once the games complete.
    """
    log = {"schema": 1, "entries": []}
    if os.path.exists(PROP_LOG_PATH):
        with open(PROP_LOG_PATH, encoding="utf-8") as f:
            log = json.load(f)

    seen = {(e["date"], e["player"], e["market"], e["line"], e["side"]) for e in log["entries"]}

    added = 0
    for p in props:
        key = (date_str, p.get("player"), p.get("market"), p.get("line"), p.get("side"))
        if key in seen:
            continue
        log["entries"].append({
            "date": date_str,
            "game": p.get("game"),
            "player": p.get("player"),
            "market": p.get("market"),
            "line": p.get("line"),
            "side": p.get("side"),
            "price": p.get("odds"),
            "book": p.get("book"),
            "graded": False,
            "actual_stat": None,
            "hit": None,
        })
        added += 1

    if added:
        atomic_write_json(PROP_LOG_PATH, log)
        print(f"Logged {added} new props to {PROP_LOG_PATH} ({len(log['entries'])} total)")
    else:
        print("No new props to log (already logged today, or none generated).")


def main():
    now = datetime.now(MST)
    # %-d isn't portable (glibc-only) - built from .day directly instead.
    date_label = f"{now:%A, %B} {now.day}, {now.year}"
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

    print("Fetching real prop lines...")
    events = fetch_events()
    raw_props = fetch_player_props(events)
    prop_context = build_prop_context(raw_props)

    print("Generating player props...")
    player_props = generate_player_props(prop_context, date_label)

    if not value_plays or not goalie_starts:
        print("Core sections failed - aborting")
        return
    if not player_props:
        player_props = {"props": [], "note": "Prop generation unavailable."}

    roster_names = build_roster_name_set(rosters)
    n_plays_before = len(value_plays.get("plays", []))
    value_plays = validate_players(value_plays, "plays", roster_names, "Value plays")
    goalie_starts = validate_players(goalie_starts, "goalies", roster_names, "Goalie starts")

    if n_plays_before and not value_plays.get("plays"):
        print("WARNING: every value play was dropped as off-roster - check the roster fetch")

    # Prop players come from the books, not from the model, so the prop feed is
    # authoritative for that section - a roster we truncated at 20 is not.
    prop_names = {normalize_name(p["player"]) for p in raw_props if p.get("player")}
    player_props = validate_players(player_props, "props", prop_names, "Player props")

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

    log_props(player_props.get("props", []), today)

    n_plays = len(value_plays.get("plays", []))
    n_goalies = len(goalie_starts.get("goalies", []))
    n_props = len(player_props.get("props", []))
    print(f"fantasy.json written - {n_plays} plays, {n_goalies} goalies, {n_props} props")

if __name__ == "__main__":
    main()
