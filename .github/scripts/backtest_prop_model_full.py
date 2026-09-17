#!/usr/bin/env python3
"""
Backtests generate_real_props.py's exact selection algorithm (Poisson rate
model vs market-implied probability, at several MIN_EDGE thresholds) against
the FULL odds board in data/historical_prop_odds.json - every player/market/
side offered in each logged game - rather than only the subset of picks the
Claude-based pipeline (update_fantasy.py) happened to select and log.

backtest_prop_model.py answers a narrower question: "were the picks Claude
already chose good, by this model's own standard?" This script answers
"would the 3% edge rule itself have made money, across every prop it could
have taken?" - the actual question to ask before wiring the model in.

Ground truth and prior-game rates come from data/player_game_logs.json,
which only covers players who ever appeared in data/player_props_log.json -
so this is bounded by that player set and by whichever games
build_historical_odds.py fetched odds for (games the old pipeline already
touched, not a random sample of the season). It is the fullest test possible
from data already on disk, not a clean, unbiased season-long sample - treat
the results as directional, and re-run as historical_prop_odds.json grows.

Odds were snapshotted ~20 min before puck drop (see build_historical_odds.py),
the closest proxy this repo has to real closing lines.

Writes data/prop_model_backtest_full.json.
"""
import json
import math
import statistics

MIN_GAMES_PLAYED = 10  # matches generate_real_props.py's MIN_GAMES_PLAYED
CATEGORY_TO_MARKET = {
    "goals": "player_goal_scorer_anytime",
    "points": "player_points",
    "shots": "player_shots_on_goal",
    "assists": "player_assists",
}
THRESHOLDS = [0.0, 0.03, 0.05, 0.08, 0.10, 0.15]


def normalize_name(name):
    return name.strip().lower().replace(".", "").replace("'", "").replace("-", " ")


def poisson_cdf(k, lam):
    """P(X <= k) for Poisson(lam)"""
    if lam <= 0:
        return 1.0
    total = 0.0
    for i in range(0, k + 1):
        total += math.exp(-lam) * (lam ** i) / math.factorial(i)
    return total


def implied_prob(american_odds):
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return abs(american_odds) / (abs(american_odds) + 100)


def parse_odds_profit(american_odds):
    """Returns (profit_if_win, risk) normalized to $100-equivalent stake."""
    if american_odds > 0:
        return american_odds, 100
    return 100, abs(american_odds)


def build_lookups(game_logs):
    """name -> games list, and (name, date) -> that game's stat row."""
    by_name = {}
    stat_lookup = {}
    for entry in game_logs.values():
        key = normalize_name(entry["name"])
        by_name[key] = entry["games"]
        for g in entry["games"]:
            stat_lookup[(key, g["date"])] = g
    return by_name, stat_lookup


def market_lines_for_game(bookmakers):
    """(player_key, category) -> (player_name, bookmaker, point, price_a, price_b).

    Takes the first bookmaker offering each player/category, matching
    generate_real_props.py's find_market_lines()+lines[0] behavior exactly,
    but scans every player in the game instead of one player at a time.
    """
    per_player_category = {}
    for bk in bookmakers:
        for market in bk.get("markets", []):
            category = next((c for c, m in CATEGORY_TO_MARKET.items() if m == market["key"]), None)
            if category is None:
                continue
            outcomes = market.get("outcomes", [])
            if category == "goals":
                for o in outcomes:
                    if o.get("name") != "Yes":
                        continue
                    pkey = normalize_name(o.get("description", ""))
                    k = (pkey, category)
                    per_player_category.setdefault(k, (o.get("description", ""), bk["title"], None, o["price"], None))
            else:
                grouped = {}
                for o in outcomes:
                    pkey = normalize_name(o.get("description", ""))
                    grouped.setdefault(pkey, {"name": o.get("description", "")})
                    if o["name"] == "Over":
                        grouped[pkey]["over"] = o["price"]
                        grouped[pkey]["point"] = o.get("point")
                    elif o["name"] == "Under":
                        grouped[pkey]["under"] = o["price"]
                for pkey, v in grouped.items():
                    if "over" not in v or "under" not in v:
                        continue
                    k = (pkey, category)
                    per_player_category.setdefault(k, (v["name"], bk["title"], v["point"], v["over"], v["under"]))
    return per_player_category


def evaluate_candidate(category, pname, bookmaker, point, price_a, price_b, rate, n_prior, actual_stat, date_str, game_label):
    if category == "goals":
        model_prob = 1 - math.exp(-rate)
        market_prob = implied_prob(price_a)
        edge = model_prob - market_prob
        return {
            "date": date_str, "game": game_label, "player": pname, "category": category,
            "pick_side": "goal_scorer", "line": None, "model_prob": round(model_prob, 3),
            "market_prob": round(market_prob, 3), "edge": round(edge, 3), "odds": price_a,
            "bookmaker": bookmaker, "n_prior_games": n_prior, "actual_won": actual_stat >= 1,
            "push": False,
        }

    floor_line = int(math.floor(point))
    p_under = poisson_cdf(floor_line, rate)
    p_over = 1 - p_under
    over_market = implied_prob(price_a)
    under_market = implied_prob(price_b)
    edge_over = p_over - over_market
    edge_under = p_under - under_market

    if edge_over >= edge_under:
        side, model_prob, market_prob, edge, odds = "over", p_over, over_market, edge_over, price_a
        actual_won = actual_stat > point
    else:
        side, model_prob, market_prob, edge, odds = "under", p_under, under_market, edge_under, price_b
        actual_won = actual_stat < point

    return {
        "date": date_str, "game": game_label, "player": pname, "category": category,
        "pick_side": side, "line": point, "model_prob": round(model_prob, 3),
        "market_prob": round(market_prob, 3), "edge": round(edge, 3), "odds": odds,
        "bookmaker": bookmaker, "n_prior_games": n_prior, "actual_won": actual_won,
        "push": actual_stat == point,
    }


def backtest_at_threshold(entries, threshold):
    picks = [p for p in entries if p["edge"] >= threshold and not p["push"]]
    n = len(picks)
    if n == 0:
        return {"picks": 0, "wins": 0, "win_rate": 0.0, "roi": 0.0, "roi_ci95": [0.0, 0.0]}
    wins = sum(1 for p in picks if p["actual_won"])
    total_profit, total_risk = 0.0, 0.0
    per_bet_returns = []
    for p in picks:
        profit_if_win, risk = parse_odds_profit(p["odds"])
        total_risk += risk
        pnl = profit_if_win if p["actual_won"] else -risk
        total_profit += pnl
        per_bet_returns.append(pnl / risk * 100)
    roi = (total_profit / total_risk * 100) if total_risk > 0 else 0.0
    if n > 1:
        se = statistics.stdev(per_bet_returns) / math.sqrt(n)
        mean_ret = statistics.mean(per_bet_returns)
        ci = [round(mean_ret - 1.96 * se, 1), round(mean_ret + 1.96 * se, 1)]
    else:
        ci = [None, None]
    return {"picks": n, "wins": wins, "win_rate": round(wins / n * 100, 1), "roi": round(roi, 1), "roi_ci95": ci}


def main():
    with open("data/historical_prop_odds.json") as f:
        historical_odds = json.load(f)
    with open("data/player_game_logs.json") as f:
        game_logs = json.load(f)

    by_name, stat_lookup = build_lookups(game_logs)

    candidates = []
    players_no_log = set()

    for odds_key, odds_entry in historical_odds.items():
        date_str, game_label = odds_key.split("|", 1)
        bookmakers = odds_entry.get("data", {}).get("bookmakers", [])

        for (pkey, category), (pname, bookmaker, point, price_a, price_b) in market_lines_for_game(bookmakers).items():
            if pkey not in by_name:
                players_no_log.add(pname)
                continue

            prior = [g for g in by_name[pkey] if g["date"] < date_str]
            if len(prior) < MIN_GAMES_PLAYED:
                continue
            rate = sum(g[category] for g in prior) / len(prior)

            actual_row = stat_lookup.get((pkey, date_str))
            if actual_row is None:
                continue  # not on record as having played that date - skip rather than guess

            candidates.append(evaluate_candidate(
                category, pname, bookmaker, point, price_a, price_b,
                rate, len(prior), actual_row[category], date_str, game_label,
            ))

    print(f"Games in historical_prop_odds.json: {len(historical_odds)}")
    print(f"Players seen in odds with no game-log coverage (skipped): {len(players_no_log)}")
    print(f"Full-universe candidates (>= {MIN_GAMES_PLAYED} prior GP, graded): {len(candidates)}\n")

    print(f"{'Min Edge':<12}{'Picks':<8}{'Wins':<7}{'Win Rate':<12}{'ROI':<10}{'95% CI'}")
    threshold_results = {}
    for t in THRESHOLDS:
        r = backtest_at_threshold(candidates, t)
        threshold_results[f"edge_{int(t*100)}pct"] = r
        ci = f"[{r['roi_ci95'][0]:+.1f}%, {r['roi_ci95'][1]:+.1f}%]" if r["roi_ci95"][0] is not None else "n/a"
        print(f"{int(t*100)}%+{'':<9}{r['picks']:<8}{r['wins']:<7}{r['win_rate']}%{'':<7}{r['roi']:+.1f}%{'':<5}{ci}")

    print("\nBy category, at the 3% threshold generate_real_props.py actually uses:")
    by_category = {}
    picks_3pct = [c for c in candidates if c["edge"] >= 0.03 and not c["push"]]
    for cat in CATEGORY_TO_MARKET:
        cat_picks = [p for p in picks_3pct if p["category"] == cat]
        r = backtest_at_threshold(cat_picks, 0.0)
        by_category[cat] = r
        print(f"  {cat:<10} picks={r['picks']:<5} win_rate={r['win_rate']}%  roi={r['roi']:+.1f}%")

    output = {
        "games_seen": len(historical_odds),
        "players_without_game_log": len(players_no_log),
        "total_candidates": len(candidates),
        "backtest_by_edge_threshold": threshold_results,
        "edge_3pct_by_category": by_category,
        "all_candidates": candidates,
    }
    with open("data/prop_model_backtest_full.json", "w") as f:
        json.dump(output, f, indent=2)
    print("\nWrote data/prop_model_backtest_full.json")


if __name__ == "__main__":
    main()
