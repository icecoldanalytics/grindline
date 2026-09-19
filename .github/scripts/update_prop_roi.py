#!/usr/bin/env python3
"""
Grades every logged player prop pick in data/prop_log.json against real
NHL box scores, then computes n / hit rate / ROI / 95% CI - overall and
per market - and writes data/prop_roi.json.

This is the props equivalent of update_roi.py: capture_signals.py logs
picks as they're made (data/signal_log.json), update_roi.py grades and
reports on them once results are known. Here, update_fantasy.py's
log_props() logs picks as they're made (data/prop_log.json, following
the same pattern), and this script does the grading and reporting -
run it after update_fantasy.py, once games have had time to finish.

Not named backtest_prop_*.py on purpose, despite computing backtest-style
stats: backtest_prop_model.py / backtest_prop_model_full.py are one-off
historical reconstructions of generate_real_props.py's edge-model
algorithm against every historical game. This script grades actual
logged picks incrementally as they resolve, the same ongoing role
update_roi.py plays for Rest Edge - re-running this file name would
invite exactly the producer/consumer confusion this script's own
sibling task was written to catch.

Grading source: NHL box scores, player stats matched by (first initial,
last name) since the boxscore API returns abbreviated names ("M. Domi")
against the log's full names ("Max Domi") - same matching approach
grade_prop_picks.py already uses successfully for the older (now
superseded) player_props_log.json.

Market -> stat mapping:
  player_shots_on_goal -> shots on goal, Over/Under vs. line
  player_points        -> points, Over/Under vs. line
  player_assists       -> assists, Over/Under vs. line
  player_goal_scorer_anytime -> goals >= 1, side is "Yes" (no line)

ROI/CI method matches backtest_rest_signals.py: dollar-weighted ROI
across picks; 95% CI from the per-bet (pnl/risk*100) return distribution
(mean +/- 1.96 * SE). A win pays the posted American price; a loss costs
the stake - same math as calc_bucket() elsewhere in this repo, just
keyed to hit/miss instead of home_won.

Run:  python .github/scripts/update_prop_roi.py
"""
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

LOG_PATH = "data/prop_log.json"
OUTPUT_PATH = "data/prop_roi.json"
REQUEST_SLEEP = 0.3

MIN_SAMPLE = 20


def atomic_write_json(path, data, indent=2):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


def load_json_or_fail(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"\n{path} exists but will not parse as JSON ({e}). Refusing to "
            f"continue - check {path}.bak before rerunning."
        )


def normalize_name(name):
    return (name or "").strip().lower().replace(".", "").replace("'", "")


def name_key(name):
    """(first_initial, last_name) - matches boxscore's abbreviated names
    ("M. Domi") against the log's full names ("Max Domi")."""
    clean = normalize_name(name)
    parts = clean.split()
    if not parts:
        return ("", "")
    return (parts[0][0], parts[-1])


def get_game_id(date_str, away, home):
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/score/{date_str}", timeout=15)
        r.raise_for_status()
        data = r.json()
        for g in data.get("games", []):
            if g["awayTeam"]["abbrev"] == away and g["homeTeam"]["abbrev"] == home:
                if g.get("gameState") in ("OFF", "FINAL"):
                    return g["id"]
                return None  # not final yet - leave ungraded for a later run
        return None
    except Exception as e:
        print(f"  score fetch error {away}@{home} {date_str}: {e}")
        return None


def get_boxscore_stats(game_id):
    """{(first_initial, last_name): {goals, assists, points, shots}}"""
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore", timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  boxscore error for game {game_id}: {e}")
        return {}

    stats = {}
    pbs = data.get("playerByGameStats", {})
    for side in ("awayTeam", "homeTeam"):
        for group in ("forwards", "defense"):
            for p in pbs.get(side, {}).get(group, []):
                name = p.get("name", {}).get("default", "")
                if not name:
                    continue
                goals = p.get("goals", 0)
                assists = p.get("assists", 0)
                points = p.get("points", goals + assists)
                shots = p.get("sog", p.get("shots", 0))
                stats[name_key(name)] = {"goals": goals, "assists": assists,
                                          "points": points, "shots": shots}
    return stats


MARKET_STAT = {
    "player_shots_on_goal": "shots",
    "player_points": "points",
    "player_assists": "assists",
}


def grade_one(entry, player_stats):
    market = entry["market"]
    side = (entry.get("side") or "").strip().lower()

    if market == "player_goal_scorer_anytime":
        actual = player_stats.get("goals")
        if actual is None:
            return None, None
        hit = actual >= 1 if side == "yes" else actual == 0
        return actual, hit

    stat_key = MARKET_STAT.get(market)
    if stat_key is None:
        return None, None
    actual = player_stats.get(stat_key)
    line = entry.get("line")
    if actual is None or line is None:
        return None, None
    line = float(line)
    if side == "over":
        hit = actual > line
    elif side == "under":
        hit = actual < line
    else:
        return None, None
    return actual, hit


def parse_odds_profit(price_str):
    val = float(str(price_str).replace("+", ""))
    if val > 0:
        return val, 100
    return 100, abs(val)


def calc_bucket(entries):
    n = len(entries)
    if n == 0:
        return {"n": 0, "wins": 0, "hit_rate": None, "roi": None, "roi_ci95": [None, None]}
    wins = sum(1 for e in entries if e["hit"])
    total_profit, total_risk, per_bet = 0.0, 0.0, []
    for e in entries:
        profit_if_win, risk = parse_odds_profit(e["price"])
        pnl = profit_if_win if e["hit"] else -risk
        total_profit += pnl
        total_risk += risk
        per_bet.append(pnl / risk * 100)
    roi = total_profit / total_risk * 100
    if n > 1:
        se = statistics.stdev(per_bet) / math.sqrt(n)
        mean_ret = statistics.mean(per_bet)
        ci = [round(mean_ret - 1.96 * se, 1), round(mean_ret + 1.96 * se, 1)]
    else:
        ci = [None, None]
    return {
        "n": n, "wins": wins, "hit_rate": round(wins / n * 100, 1),
        "roi": round(roi, 1), "roi_ci95": ci, "sample_ok": n >= MIN_SAMPLE,
    }


def fmt(label, r):
    if r["n"] == 0:
        print(f"  {label:<24} n=0")
        return
    ci = r["roi_ci95"]
    ci_str = f"[{ci[0]:+.1f}%, {ci[1]:+.1f}%]" if ci[0] is not None else "n/a"
    print(f"  {label:<24} n={r['n']:<5} wins={r['wins']:<5} hit_rate={r['hit_rate']:>5.1f}%  "
          f"roi={r['roi']:+.1f}%  95% CI={ci_str}")


def main():
    if not os.path.exists(LOG_PATH):
        # Not an error: this is the normal state until update_fantasy.py has
        # logged its first real props (e.g. before the season starts, or on
        # any night no props were generated). Exit cleanly so a scheduled
        # workflow step doesn't fail daily on an expected, non-error state.
        print(f"{LOG_PATH} not found - nothing logged yet. Skipping.")
        return
    log = load_json_or_fail(LOG_PATH)

    pending = [e for e in log["entries"] if not e.get("graded")]
    if pending:
        print(f"Grading {len(pending)} pending props...")
        by_game = defaultdict(list)
        for e in pending:
            game = e.get("game") or ""
            if "@" not in game:
                continue
            away, home = [t.strip() for t in game.split("@", 1)]
            by_game[(e["date"], away, home)].append(e)

        graded_count = 0
        for i, ((date_str, away, home), entries) in enumerate(by_game.items()):
            game_id = get_game_id(date_str, away, home)
            if game_id is None:
                continue  # not final yet, or not found - leave ungraded
            stats = get_boxscore_stats(game_id)
            time.sleep(REQUEST_SLEEP)
            for e in entries:
                player_stats = stats.get(name_key(e["player"]))
                if player_stats is None:
                    print(f"  no boxscore match for {e['player']} ({date_str})")
                    continue
                actual, hit = grade_one(e, player_stats)
                if hit is None:
                    continue
                e["actual_stat"] = actual
                e["hit"] = hit
                e["graded"] = True
                graded_count += 1
            if (i + 1) % 20 == 0:
                print(f"  ...{i + 1}/{len(by_game)} games")

        atomic_write_json(LOG_PATH, log)
        print(f"  Graded {graded_count} new props.\n")
    else:
        print("Nothing pending to grade.\n")

    graded = [e for e in log["entries"] if e.get("graded")]
    overall = calc_bucket(graded)

    by_market = {}
    for market in sorted({e["market"] for e in graded}):
        by_market[market] = calc_bucket([e for e in graded if e["market"] == market])

    output = {
        "total_logged": len(log["entries"]),
        "total_graded": len(graded),
        "pending": len(log["entries"]) - len(graded),
        "overall": overall,
        "by_market": by_market,
    }
    atomic_write_json(OUTPUT_PATH, output)

    print("=" * 66)
    print("PROP PICK PERFORMANCE")
    print("=" * 66)
    fmt("OVERALL", overall)
    print()
    for market, r in by_market.items():
        fmt(market, r)
    print(f"\n✓ Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
