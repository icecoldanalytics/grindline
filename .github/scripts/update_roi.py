#!/usr/bin/env python3
"""
Builds data/roi.json from data/signal_log.json.

Every ROI figure here is graded against the REAL home moneyline that was
available when capture_signals.py's morning job actually captured it that
day, stored per game in the signal log along with the exact capture time
(price_source). Never assumed to be a fixed time like 7 AM - confirmed
live that the job's actual run time varies by hours day to day, so that
claim would itself have been an assumption. Nothing is priced by
assumption.

Three jobs:
  1. Grade any log entries that don't yet have a final score (fetches only
     the dates that need it, so this stays cheap).
  2. For Emerging Edge (data/emerging_edge_log.json) specifically: resolve
     which bucket each pending entry belongs to (away started its #1
     goalie vs. a backup) once that game's boxscore exists -
     capture_signals.py can't know this before puck drop, so it logs
     away_starter/goalie_bucket null and this fills them in later.
  3. Compute stats per bucket and write roi.json.

Runs nightly via GitHub Actions.
"""

import json
import os
import sys
from datetime import datetime, timedelta

import pytz
import requests

from rest_edge import breakeven, profit

# Windows' console defaults to cp1252, which can't encode the checkmark
# used in the summary print below; GitHub Actions' ubuntu runners default
# to UTF-8 already, so this only matters for local runs.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

MST = pytz.timezone("America/Edmonton")
LOG_PATH = os.path.join("data", "signal_log.json")
EMERGING_LOG_PATH = os.path.join("data", "emerging_edge_log.json")
GOALIE_LIVE_CACHE_PATH = os.path.join("data", "goalie_starts_live_cache.json")
OUT_PATH = os.path.join("data", "roi.json")
BACKTEST_PATH = os.path.join("data", "rest_signal_backtest.json")
SEASON = "2025-26"

# 2026-27 regular season opener - bump this each year the same way
# build_player_hub.py's SEASON_START does. The live goalie-starts cache
# only ever covers ONE season (no season field in its keys) - archive or
# clear data/goalie_starts_live_cache.json when this rolls over, or a new
# season's cumulative-starts count would be contaminated by last season's.
EMERGING_SEASON_START = "2026-09-29"
# A start only counts toward "who's the #1" once the team has played this
# many games this season - matches backtest_goalie_signal.py exactly, so
# the live bucket assignment uses the identical rule the backtest itself
# validated, not a different live-only threshold.
MIN_TEAM_GAMES = 10
# Emerging Edge's stated retirement test: if the live number-one-goalie
# sample's first 100 games run net negative ROI, retire it publicly - the
# same predetermined-threshold approach Signal 1 was retired under.
RETIREMENT_SAMPLE = 100

# A bucket needs this many games before its ROI is treated as meaningful
MIN_SAMPLE = 30


def atomic_write_json(path, data, indent=2):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


# ── Log I/O ───────────────────────────────────────────────────────────────
def load_log():
    if not os.path.exists(LOG_PATH):
        raise SystemExit(
            f"{LOG_PATH} not found. Run seed_signal_log.py first."
        )
    with open(LOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_log(log):
    atomic_write_json(LOG_PATH, log)


def load_emerging_log():
    if os.path.exists(EMERGING_LOG_PATH):
        with open(EMERGING_LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"schema": 1, "entries": []}


def save_emerging_log(log):
    atomic_write_json(EMERGING_LOG_PATH, log)


def load_goalie_live_cache():
    if os.path.exists(GOALIE_LIVE_CACHE_PATH):
        with open(GOALIE_LIVE_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_goalie_live_cache(cache):
    atomic_write_json(GOALIE_LIVE_CACHE_PATH, cache)


def fetch_scores(date_str):
    """Final scores for a date, keyed by (away, home)."""
    try:
        r = requests.get(
            f"https://api-web.nhle.com/v1/score/{date_str}", timeout=15
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  Could not fetch {date_str}: {e}")
        return {}

    out = {}
    for g in data.get("games", []):
        if g.get("gameState") not in ("OFF", "FINAL"):
            continue
        key = (g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"])
        out[key] = (
            g["awayTeam"].get("score", 0),
            g["homeTeam"].get("score", 0),
        )
    return out


def grade_pending(log):
    """Fill in scores for any ungraded entries. Returns count newly graded."""
    pending = [e for e in log["entries"] if not e.get("graded")]
    if not pending:
        return 0

    dates = sorted({e["date"] for e in pending})
    print(f"Grading {len(pending)} pending games across {len(dates)} dates...")

    graded = 0
    for ds in dates:
        scores = fetch_scores(ds)
        for e in pending:
            if e["date"] != ds:
                continue
            key = (e["away"], e["home"])
            if key not in scores:
                continue
            a, h = scores[key]
            e["away_score"] = a
            e["home_score"] = h
            e["fade_won"] = h > a
            e["graded"] = True
            graded += 1
    return graded


# ── Emerging Edge: live goalie-starts tracking ──────────────────────────────
def parse_toi(toi_str):
    try:
        m, s = toi_str.split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return 0


def get_starters(game_id):
    """(away_starter_name, home_starter_name) from the boxscore - checks
    the starter flag first, falls back to whoever logged the most
    time-on-ice if it's missing. Identical logic to
    backtest_goalie_signal.py's get_starters() (confirmed live there that
    the flag is present in 100% of a 24-game sample spread across four
    historical seasons; this fallback exists for the rare game where it
    isn't, never invented)."""
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore", timeout=15)
        r.raise_for_status()
        box = r.json()
    except Exception as e:
        print(f"    boxscore error for game {game_id}: {e}")
        return None, None

    pbs = box.get("playerByGameStats", {})
    result = {}
    for side in ("awayTeam", "homeTeam"):
        goalies = pbs.get(side, {}).get("goalies", [])
        starter = next((g for g in goalies if g.get("starter") is True), None)
        if starter is None and goalies:
            starter = max(goalies, key=lambda g: parse_toi(g.get("toi", "0:00")))
        result[side] = starter.get("name", {}).get("default") if starter else None
    return result.get("awayTeam"), result.get("homeTeam")


def update_goalie_live_cache(cache):
    """Fetches starters for every completed regular-season game from
    EMERGING_SEASON_START through today that isn't already cached - not
    just Emerging Edge candidate games, since determining "is this team's
    #1" needs every start the team has made this season, the same way
    backtest_goalie_signal.py needs a full-season reconstruction rather
    than just its own signal games. Incremental: a normal day-to-day run
    only fetches boxscores for yesterday's (and today's, if already
    final) new games - re-queries the score list for already-done dates
    too (cheap, one call each) but skips any game already in the cache."""
    mst_today = datetime.now(MST).date()
    start = datetime.strptime(EMERGING_SEASON_START, "%Y-%m-%d").date()
    if mst_today < start:
        return cache, 0

    new_games = 0
    d = start
    while d <= mst_today:
        ds = d.strftime("%Y-%m-%d")
        try:
            r = requests.get(f"https://api-web.nhle.com/v1/score/{ds}", timeout=15)
            r.raise_for_status()
            games = r.json().get("games", [])
        except Exception as e:
            print(f"    score fetch error {ds}: {e}")
            games = []
            d += timedelta(days=1)
            continue

        for g in games:
            if g.get("gameType") != 2 or g.get("gameState") not in ("OFF", "FINAL"):
                continue
            away = g["awayTeam"]["abbrev"]
            home = g["homeTeam"]["abbrev"]
            key = f"{ds}|{away}@{home}"
            if key in cache:
                continue
            away_starter, home_starter = get_starters(g["id"])
            cache[key] = {"away_starter": away_starter, "home_starter": home_starter}
            new_games += 1
        d += timedelta(days=1)

    if new_games:
        save_goalie_live_cache(cache)
        print(f"  Goalie live cache: {new_games} new games fetched ({len(cache)} total).")
    return cache, new_games


def resolve_pending_emerging_edge(elog, goalie_cache):
    """For every Emerging Edge entry still missing a goalie_bucket, walks
    every cached game THIS SEASON in chronological order, tracking each
    team's cumulative starts, and resolves the entry using ONLY the counts
    that existed BEFORE that game (no hindsight) - identical method and
    MIN_TEAM_GAMES threshold to backtest_goalie_signal.py. A start counts
    as "backup" only once the away team has played MIN_TEAM_GAMES games
    this season (needs a clear #1 to compare against) AND that start has
    strictly fewer cumulative starts than the team's leader; earlier
    starts are marked "excluded" (still graded for the record, just left
    out of both buckets' stats) rather than guessed at. A game whose
    boxscore doesn't exist in the cache yet (not final, or capture ran
    faster than the boxscore populated) is simply left pending for a
    later run."""
    entries_by_key = {(e["date"], e["away"], e["home"]): e for e in elog["entries"]}
    pending_keys = {k for k, e in entries_by_key.items() if e.get("goalie_bucket") is None}
    if not pending_keys:
        return 0

    starts = {}       # team -> {starter_name: count}
    team_games = {}    # team -> games played so far this season
    resolved = 0

    for cache_key in sorted(goalie_cache.keys()):
        date_str, teams = cache_key.split("|", 1)
        away, home = teams.split("@")
        info = goalie_cache[cache_key]
        away_starter = info.get("away_starter")
        home_starter = info.get("home_starter")

        pkey = (date_str, away, home)
        if pkey in pending_keys and away_starter:
            entry = entries_by_key[pkey]
            gp = team_games.get(away, 0)
            team_starts = starts.get(away, {})
            entry["away_starter"] = away_starter
            if gp >= MIN_TEAM_GAMES and team_starts:
                leader = max(team_starts.values())
                starter_count = team_starts.get(away_starter, 0)
                entry["goalie_bucket"] = "backup" if starter_count < leader else "number_one"
            else:
                entry["goalie_bucket"] = "excluded"
            resolved += 1

        for team, starter in ((away, away_starter), (home, home_starter)):
            team_games[team] = team_games.get(team, 0) + 1
            if starter:
                starts.setdefault(team, {})
                starts[team][starter] = starts[team].get(starter, 0) + 1

    return resolved


def retirement_check(number_one_entries):
    """Emerging Edge's stated retirement test: if the live number-one-
    goalie sample's first RETIREMENT_SAMPLE games (chronologically) run
    net negative ROI, status is "retire" - published on the site either
    way, the same predetermined-threshold approach Signal 1 was retired
    under (backtest_rest_signals.py)."""
    first = number_one_entries[:RETIREMENT_SAMPLE]
    roi_so_far = calc_stats(first)["roi"] if first else None
    if len(first) < RETIREMENT_SAMPLE:
        return {
            "games_so_far": len(first), "games_needed": RETIREMENT_SAMPLE,
            "status": "pending", "roi_so_far": roi_so_far,
        }
    return {
        "games_so_far": RETIREMENT_SAMPLE, "games_needed": RETIREMENT_SAMPLE,
        "status": "retire" if roi_so_far < 0 else "pass", "roi_so_far": roi_so_far,
    }


# ── Maths ─────────────────────────────────────────────────────────────────
def calc_stats(entries):
    """Stats for a bucket, graded at each game's own real price."""
    n = len(entries)
    if n == 0:
        return {
            "games": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "roi": 0.0, "avg_odds": 0, "breakeven_rate": 0.0,
            "sd_above_breakeven": 0.0, "sample_ok": False,
        }

    wins = sum(1 for e in entries if e["fade_won"])
    total = sum(profit(e["home_ml_avg"], e["fade_won"]) for e in entries)
    avg_odds = sum(e["home_ml_avg"] for e in entries) / n
    win_rate = wins / n * 100
    be = breakeven(avg_odds)
    se = (0.5 * 0.5 / n) ** 0.5 * 100

    return {
        "games": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(win_rate, 1),
        "roi": round(total / (n * 100) * 100, 1),
        "avg_odds": round(avg_odds, 1),
        "breakeven_rate": round(be, 1),
        "sd_above_breakeven": round((win_rate - be) / se, 1) if se else 0.0,
        "sample_ok": n >= MIN_SAMPLE,
    }


def calc_monthly(entries):
    months = {}
    for e in entries:
        m = months.setdefault(e["date"][:7], {"wins": 0, "losses": 0, "profit": 0.0})
        if e["fade_won"]:
            m["wins"] += 1
        else:
            m["losses"] += 1
        m["profit"] += profit(e["home_ml_avg"], e["fade_won"])

    out = []
    for month, d in sorted(months.items()):
        n = d["wins"] + d["losses"]
        out.append({
            "month": month,
            "wins": d["wins"],
            "losses": d["losses"],
            "roi": round(d["profit"] / (n * 100) * 100, 1) if n else 0.0,
        })
    return out


def calc_streak(entries):
    if not entries:
        return "—"
    last = entries[-1]["fade_won"]
    streak = 0
    for e in reversed(entries):
        if e["fade_won"] != last:
            break
        streak += 1
    return f"{streak}{'W' if last else 'L'}"


def best_month(monthly, min_games=8):
    """Best month, ignoring tiny samples that top the table on noise."""
    eligible = [m for m in monthly if m["wins"] + m["losses"] >= min_games]
    if not eligible:
        return "—"
    b = max(eligible, key=lambda m: m["roi"])
    return datetime.strptime(b["month"], "%Y-%m").strftime("%b %Y")


def cumulative(entries):
    """Running profit in units, for a season-review chart."""
    running, out = 0.0, []
    for e in entries:
        running += profit(e["home_ml_avg"], e["fade_won"]) / 100
        out.append({"date": e["date"], "units": round(running, 2)})
    return out


def format_price_time(price_source):
    """Human-readable label for a logged entry's price_source - either a
    real live capture ("live_HH:MM_MT", the exact clock time
    capture_signals.py actually pulled odds that run) or a fixed
    historical backtest snapshot ("historical_14utc", used only for the
    pre-live-tracking backfilled games and never a claim about a real
    capture moment). Surfacing this per game is the honest alternative to
    a single "captured at 7 AM" claim that wasn't true."""
    if not price_source:
        return "—"
    if price_source.startswith("live_") and price_source.endswith("_MT"):
        hhmm = price_source[len("live_"):-len("_MT")]
        try:
            dt = datetime.strptime(hhmm, "%H:%M")
            return dt.strftime("%-I:%M %p MT")
        except ValueError:
            return price_source
    if price_source.startswith("historical_"):
        return "backtest snapshot"
    return price_source


def load_retired_signal1():
    """Frozen historical documentation for the retired Signal 1 condition
    (away B2B, home rested 3+ days) - backtest_rest_signals.py's pooled
    result at real closing moneylines across all four backfilled seasons.
    Not re-derived from signal_log.json: capture_signals.py stopped logging
    these games once Signal 1 was retired, so there's nothing left to grade
    live."""
    try:
        with open(BACKTEST_PATH, encoding="utf-8") as f:
            backtest = json.load(f)
    except FileNotFoundError:
        return {"games": 0, "win_rate": None, "roi": None, "note": "backtest not found"}
    pooled = backtest["signal1_retired"]["pooled"]
    return {
        "games": pooled["n"],
        "win_rate": pooled["win_rate"],
        "roi": pooled["roi"],
        "roi_ci95": pooled["roi_ci95"],
        "label": "Away B2B + Home Rested 3+ Days",
        "status": "Retired — no edge found",
        "source": "backtest_rest_signals.py, real closing moneylines, 2022-23 through 2025-26",
    }


def emerging_edge_block(elog):
    """Grades, resolves, and summarizes Emerging Edge - separately from
    Rest Edge's signal_log.json/rest_edge_block, per its own bucket."""
    goalie_cache = load_goalie_live_cache()
    goalie_cache, new_starts = update_goalie_live_cache(goalie_cache)

    resolved = resolve_pending_emerging_edge(elog, goalie_cache)
    if resolved:
        print(f"  Emerging Edge: resolved {resolved} pending goalie bucket(s).")

    newly_graded = grade_pending(elog)
    if resolved or newly_graded:
        save_emerging_log(elog)
        if newly_graded:
            print(f"  Emerging Edge: graded {newly_graded} new game(s).")

    graded_entries = sorted(
        [e for e in elog["entries"] if e.get("graded")],
        key=lambda e: e["date"],
    )
    number_one = [e for e in graded_entries if e.get("goalie_bucket") == "number_one"]
    backup = [e for e in graded_entries if e.get("goalie_bucket") == "backup"]
    excluded_count = sum(1 for e in graded_entries if e.get("goalie_bucket") == "excluded")
    pending_count = sum(1 for e in elog["entries"] if e.get("goalie_bucket") is None)

    s_number_one = calc_stats(number_one)
    s_backup = calc_stats(backup)
    m_number_one = calc_monthly(number_one)

    return {
        "label": "Away B2B + Home Rested (Any Amount, Not B2B) + Away Started Its #1 Goalie",
        "what_emerging_means": (
            "Fewer games than Rest Edge, and the 95% confidence interval on its "
            "backtested ROI still includes zero - it is not yet validated the way "
            "Rest Edge is. It's tracked live from opening night regardless, and "
            "results are published here either way, win or lose."
        ),
        "retirement_test": retirement_check(number_one),
        "number_one": {
            **s_number_one,
            "streak": calc_streak(number_one),
            "monthly": m_number_one,
            "cumulative": cumulative(number_one),
        },
        "backup_comparison": s_backup,
        "excluded_pre_min_games": excluded_count,
        "pending_goalie_resolution": pending_count,
        "last5": [{
            "date": e["date"], "away": e["away"], "home": e["home"],
            "score": f"{e['away_score']}-{e['home_score']}",
            "odds": e["home_ml_avg"], "fade_won": e["fade_won"],
            "away_starter": e.get("away_starter"),
            "price_time": format_price_time(e.get("price_source")),
        } for e in number_one[-5:]],
    }


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    log = load_log()
    newly = grade_pending(log)
    if newly:
        save_log(log)
        print(f"  Graded {newly} new games.\n")

    entries = sorted(
        [e for e in log["entries"] if e.get("graded")],
        key=lambda e: e["date"],
    )

    rest_edge = [e for e in entries if e["signal"] == "rest_edge"]

    s_rest_edge = calc_stats(rest_edge)

    m_rest_edge = calc_monthly(rest_edge)

    print("Processing Emerging Edge...")
    emerging_elog = load_emerging_log()
    emerging_block = emerging_edge_block(emerging_elog)

    last5 = [{
        "date": e["date"],
        "away": e["away"],
        "home": e["home"],
        "score": f"{e['away_score']}-{e['home_score']}",
        "odds": e["home_ml_avg"],
        "fade_won": e["fade_won"],
        "price_time": format_price_time(e.get("price_source")),
    } for e in rest_edge[-5:]]

    through = entries[-1]["date"] if entries else "—"

    rest_edge_block = {
        **s_rest_edge,
        "label": "Away B2B + Home Rested 2 Days",
        "streak": calc_streak(rest_edge),
        "best_month": best_month(m_rest_edge),
        "monthly": m_rest_edge,
        "cumulative": cumulative(rest_edge),
        "status": "Active" if s_rest_edge["roi"] > 0 and s_rest_edge["sample_ok"] else "Monitoring",
    }
    output = {
        "generated": datetime.now(MST).strftime("%Y-%m-%d %I:%M %p MT"),
        "season": SEASON,
        "through_date": through,
        "pricing": {
            "method": "Real home moneyline, average across every US bookmaker the "
                      "Odds API returns for that game (typically 9-11 books, not a "
                      "fixed list — see rest_edge.home_ml_average), captured by the "
                      "morning capture job. Never a fixed assumed time like 7 AM — "
                      "each game's exact capture time is logged and shown per game "
                      "in the results below.",
            "assumed_odds_used": False,
        },

        "rest_edge": rest_edge_block,
        "retired_signal1": load_retired_signal1(),
        "emerging_edge": emerging_block,

        "summary": {
            "total_rest_edge_games": s_rest_edge["games"],
            "cancelled_both_b2b": log.get("cancelled_both_b2b", 0),
            "last5_rest_edge": last5,
        },
    }

    os.makedirs("data", exist_ok=True)
    atomic_write_json(OUT_PATH, output)

    print("✓ roi.json written — all ROI graded at real prices\n")
    if s_rest_edge["games"] == 0:
        print("  rest_edge      no games")
    else:
        print(f"  rest_edge      {s_rest_edge['games']:>4}g  {s_rest_edge['win_rate']:>5.1f}%  "
              f"avg {s_rest_edge['avg_odds']:+.1f}  breakeven {s_rest_edge['breakeven_rate']:.1f}%  "
              f"ROI {s_rest_edge['roi']:+.1f}%  ({s_rest_edge['sd_above_breakeven']:+.1f} SD)")
    s_num1 = emerging_block["number_one"]
    if s_num1["games"] == 0:
        print("  emerging_edge  no games")
    else:
        print(f"  emerging_edge  {s_num1['games']:>4}g  {s_num1['win_rate']:>5.1f}%  "
              f"avg {s_num1['avg_odds']:+.1f}  breakeven {s_num1['breakeven_rate']:.1f}%  "
              f"ROI {s_num1['roi']:+.1f}%  retirement_test={emerging_block['retirement_test']['status']}")


if __name__ == "__main__":
    main()
