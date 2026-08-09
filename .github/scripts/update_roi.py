#!/usr/bin/env python3
"""
Builds data/roi.json from data/signal_log.json.

Every ROI figure here is graded against the REAL home moneyline that was
available at 7 AM MST, stored per game in the signal log. Nothing is priced
by assumption.

Two jobs:
  1. Grade any log entries that don't yet have a final score (fetches only
     the dates that need it, so this stays cheap).
  2. Compute stats per rest bucket and write roi.json.

Runs nightly via GitHub Actions.
"""

import json
import os
from datetime import datetime, timedelta

import pytz
import requests

MST = pytz.timezone("America/Edmonton")
LOG_PATH = os.path.join("data", "signal_log.json")
OUT_PATH = os.path.join("data", "roi.json")
SEASON = "2025-26"

# A bucket needs this many games before its ROI is treated as meaningful
MIN_SAMPLE = 30


# ── Log I/O ───────────────────────────────────────────────────────────────
def load_log():
    if not os.path.exists(LOG_PATH):
        raise SystemExit(
            f"{LOG_PATH} not found. Run seed_signal_log.py first."
        )
    with open(LOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_log(log):
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


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


# ── Maths ─────────────────────────────────────────────────────────────────
def profit(american, won):
    """Profit on a 100-unit stake at the given American price."""
    if not won:
        return -100.0
    return 100.0 * 100.0 / abs(american) if american < 0 else float(american)


def breakeven(american):
    """Win rate needed to break even at this price, as a percentage."""
    a = abs(american)
    return (a / (a + 100) * 100) if american < 0 else (100 / (american + 100) * 100)


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

    rest2 = [e for e in entries if e["signal"] == "rest2"]
    rest3 = [e for e in entries if e["signal"] == "rest3plus"]
    goalie = [e for e in rest2 + rest3 if e.get("away_started_number_one") is True]
    goalie.sort(key=lambda e: e["date"])

    s_rest2 = calc_stats(rest2)
    s_rest3 = calc_stats(rest3)
    s_goalie = calc_stats(goalie)

    m_rest2 = calc_monthly(rest2)
    m_rest3 = calc_monthly(rest3)

    last5 = [{
        "date": e["date"],
        "away": e["away"],
        "home": e["home"],
        "score": f"{e['away_score']}-{e['home_score']}",
        "odds": e["home_ml_avg"],
        "fade_won": e["fade_won"],
    } for e in rest2[-5:]]

    through = entries[-1]["date"] if entries else "—"

    rest2_block = {
        **s_rest2,
        "label": "Away B2B + Home Rested 2 Days",
        "streak": calc_streak(rest2),
        "best_month": best_month(m_rest2),
        "monthly": m_rest2,
        "cumulative": cumulative(rest2),
        "status": "Active" if s_rest2["roi"] > 0 and s_rest2["sample_ok"] else "Monitoring",
    }
    rest3_block = {
        **s_rest3,
        "label": "Away B2B + Home Rested 3+ Days",
        "streak": calc_streak(rest3),
        "best_month": best_month(m_rest3),
        "monthly": m_rest3,
        "cumulative": cumulative(rest3),
        "status": "Inactive — no edge found",
    }
    goalie_block = {
        **s_goalie,
        "label": "Signal game + away #1 goalie confirmed",
        "streak": calc_streak(goalie),
        "status": "No data" if s_goalie["games"] == 0 else "Building sample",
        "note": "Requires goalie_starts_cache.json. Judged only on starts "
                "accumulated before each game date.",
    }

    output = {
        "generated": datetime.now(MST).strftime("%Y-%m-%d %I:%M %p MT"),
        "season": SEASON,
        "through_date": through,
        "pricing": {
            "method": "Real home moneyline, average across DraftKings, FanDuel, "
                      "BetMGM and Pinnacle, at the 7 AM MST snapshot — the price "
                      "available when the daily email sends.",
            "assumed_odds_used": False,
        },

        # Primary, descriptive keys
        "rest2": rest2_block,
        "rest3plus": rest3_block,
        "goalie": goalie_block,

        # Legacy keys — same meanings the old site expects, so nothing breaks
        # while index.html is still reading them. Remove once the site moves
        # to rest2 / rest3plus.
        "signal1": rest3_block,
        "signal1_partial": rest2_block,
        "signal2": goalie_block,

        "summary": {
            "total_rest2_games": s_rest2["games"],
            "total_rest3plus_games": s_rest3["games"],
            "total_sig1_games": s_rest3["games"],
            "total_partial_games": s_rest2["games"],
            "total_sig2_games": s_goalie["games"],
            "cancelled_both_b2b": log.get("cancelled_both_b2b", 0),
            "last5_sig1": last5,
        },
    }

    os.makedirs("data", exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print("✓ roi.json written — all ROI graded at real prices\n")
    for name, s in (("rest2    ", s_rest2), ("rest3plus", s_rest3), ("goalie   ", s_goalie)):
        if s["games"] == 0:
            print(f"  {name}  no games")
            continue
        print(f"  {name}  {s['games']:>4}g  {s['win_rate']:>5.1f}%  "
              f"avg {s['avg_odds']:+.1f}  breakeven {s['breakeven_rate']:.1f}%  "
              f"ROI {s['roi']:+.1f}%  ({s['sd_above_breakeven']:+.1f} SD)")


if __name__ == "__main__":
    main()
