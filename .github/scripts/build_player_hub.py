#!/usr/bin/env python3
"""
Builds data/player_hub.json: a season-long fantasy reference for every
skater on a current 32-team NHL roster - 3 seasons of points/goals/
assists/shots-per-game, that team's own scoring context, this week's
opponents with their defensive context and last-start goalie, and a
disclosed weekly points projection.

Every skater is keyed by NHL playerId (not name) in a single top-level
"players" dict - that's the whole "ready for a saved roster" shape a
future feature needs: a user's roster becomes a list of playerIds, and
the page (or a future backend) filters this same dict to just those
keys. No accounts or storage are built here, just this structure.

PRIOR-SEASON CACHING (data/player_prior_seasons_cache.json):
The two completed prior seasons (currently 20252026, 20242025) are
frozen historical fact - they never change once the season ends. Fetching
every rostered skater's full career landing page every single day (the
original version of this script did - ~1145 calls) to re-read numbers
that can't change was unnecessary load on a free API. Now: a player's
prior-season totals are fetched via player/{id}/landing ONCE and cached
by playerId in a committed JSON file; every later run reuses the cache
and only calls the landing endpoint for playerIds missing from it (a
new call-up, a player never seen before). CURRENT-season numbers are
refreshed every run, but via club-stats/{team}/now - one call per team
(32 total) instead of one call per player - the same team-batched
endpoint roster_stats.py already uses elsewhere in this repo.

SEASON ROLLOVER: when a new NHL season starts, SEASON and PRIOR_SEASONS
below need updating (move current into prior, drop the oldest) - the
same yearly maintenance every other script's hardcoded SEASON constant
in this repo already requires, not something this caching scheme
avoids. At that point the newly-completed season's totals get folded
into the cache the same way any missing player does now.

Data sources, all real NHL API, nothing estimated beyond the disclosed
projection formula:

- roster/{team}/current: roster membership, refreshed every run (trades
  and call-ups happen). See roster_stats.py's docstring for why this
  matters - club-stats/now alone includes anyone who's ever accrued
  stats for a team, including players since traded away.

- player/{playerId}/landing: seasonTotals array, filtered to
  leagueAbbrev == "NHL" and gameTypeId == 2 - drops international
  tournaments (4 Nations/Olympics, confirmed interleaved in the same
  array) and preseason/playoff rows. A traded player can have TWO
  qualifying rows for one season (confirmed live: Tristan Jarry has
  separate 2025-26 rows for Pittsburgh and Edmonton) - summed, not just
  the first one taken. Used ONLY to seed the prior-seasons cache, not
  fetched again for a player already cached.

- club-stats/{team}/now: current-season per-skater totals for an entire
  team in one call. Carries its OWN "season" field - confirmed live
  this still reports last season's frozen numbers under that field
  until the new season's games actually start, so a team's current-
  season slot is only filled from this endpoint when its season field
  actually equals SEASON; otherwise every rostered player on that team
  correctly shows 0 games played this season rather than last season's
  total silently relabeled as this season's.

- club-schedule-season/{team}/{SEASON}: this week's games (gameType==2,
  matching build_schedule_analysis.py's convention), plus - from the
  same unfiltered fetch - the most recent completed game of ANY type
  for the last-start-goalie lookup (preseason counts there - it's the
  only signal available before the regular season starts).

- gamecenter/{game_id}/boxscore: that game's starting goalie by
  playerId. Falls back to whichever goalie logged the most TOI if
  neither is flagged starter (confirmed live: the starter flag isn't
  populated in at least some preseason boxscores). This is reported as
  "Last start" with the date it happened, not a predicted starter - the
  NHL API has no pregame confirmed-starter data, so nothing here should
  imply a prediction.

- api.nhle.com/stats/rest/en/team/summary: goals-for/game, goals-
  against/game, power-play% and penalty-kill% for all 32 teams in ONE
  call per season. Queried for the current season first; confirmed live
  that returns zero rows entirely before the regular season starts, so
  any team with no current-season games falls back to last season's
  row, tagged team_stat_season - same "label a fallback, never
  substitute it silently" rule roster_stats.py established.

PRESEASON HANDLING: the real season opens SEASON_START (2026-09-29,
confirmed against api.nhle.com/stats/rest/en/season). Before that date,
"this calendar week" genuinely has zero real games for every team - a
projections column of zeros there reads as "this player is bad," not
"no games yet." So before SEASON_START, the "week" shown is the
calendar week containing SEASON_START itself (the season-opening week,
which does have real scheduled games) rather than the current, empty
one - output carries a "preseason" flag and "season_start" date so the
page can say so plainly instead of leaving that column looking broken.

Projection formula (also shown on the page itself, not just here):
  weight_this_season   = min(games_played_this_season / 20, 1)
  blended_points_per_game = weight_this_season * this_season_ppg
                             + (1 - weight_this_season) * last_season_ppg
  projected_points_this_week = blended_points_per_game * games_this_week
Early in the season this is close to 100% last season's rate; by 20
games played this season it is entirely this season's rate. If a player
has no qualifying games in EITHER season the projection is left null
(shown as blank on the page, never a fabricated zero) - confirmed live
against 288 such players in the current roster set.

GOALIES: a separate top-level "goalies" dict, same playerId-keyed shape
philosophy as "players" but with goalie-appropriate stats instead of a
skater line: games played, games started, wins, losses, OT losses,
shutouts, save % and GAA - for this season and both prior seasons,
using the SAME prior-season cache file and cache-once-reuse-after
mechanism as skaters (extract_prior_seasons() now pulls both skater and
goalie fields from every landing row, harmlessly defaulting to 0 for
whichever fields the other player type doesn't have). Deliberately NO
projected points or projected starts figure: goalie fantasy scoring
isn't points-based, and which goalie starts on a given night isn't
predictable from anything this script can see - the real season stats
are shown and the projection machinery from the skater half of this
file is not reused for goalies at all. save_pct is derived from pooled
shots_against/goals_against (same formula as skaters' save_pct); GAA is
derived from pooled goals_against and total TOI (goals per 60 minutes),
not goals_against/games_played, which would be a different, less
standard number. Each goalie also carries games_this_week (their team's,
same source as the skater side) and last_start_date - found via that
specific goalie's OWN game log (player/{id}/game-log/{season}/{type},
gamesStarted flag), not their team's most recent game, so a backup who
hasn't played in weeks doesn't inherit the starter's date. Falls back
from regular-season to preseason game log when the regular season
hasn't started yet, same "preseason is the only signal available"
reasoning used for team context's last-start lookup. A cache entry
written before this goalie support existed is detected (missing the
"wins" key) and re-fetched once rather than silently read as zeros.

Run:  python .github/scripts/build_player_hub.py
Writes data/player_hub.json and updates data/player_prior_seasons_cache.json.
The first run (or a run after many new call-ups) still does one landing-
page call per uncached player; a normal daily run after that is roughly
32 (rosters) + 32 (schedules) + 32 (current-season club-stats) + ~12-32
(goalie boxscore/landing) + 2 (team summary) + ~60-96 (goalie last-start
game-log, one call per rostered goalie every run - not cached, since
it's a "right now" fact) calls.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from roster_stats import fetch_json_with_retry, get_current_roster
from team_names import FULL_NAMES

SEASON = "20262027"
SEASON_LABEL = "2026-27"
SEASON_START = "2026-09-29"
PRIOR_SEASONS = ["20252026", "20242025"]
PRIOR_SEASON_LABELS = {"20252026": "2025-26", "20242025": "2024-25"}
ALL_SEASON_LABELS = {SEASON: SEASON_LABEL, **PRIOR_SEASON_LABELS}
MIN_POOLED_GAMES = 20  # minimum games, pooled across both prior seasons, before a prior-season rate is shown at all
OUTPUT_PATH = os.path.join("data", "player_hub.json")
CACHE_PATH = os.path.join("data", "player_prior_seasons_cache.json")

EMPTY_SEASON = {"games_played": 0, "points": 0, "goals": 0, "assists": 0,
                 "shots": 0, "shots_against": 0, "goals_against": 0,
                 # Goalie-only fields, always present (default 0) so skater
                 # and goalie raw dicts share one shape - a skater's entry
                 # simply never has these read.
                 "games_started": 0, "wins": 0, "losses": 0, "ot_losses": 0,
                 "shutouts": 0, "toi_seconds": 0,
                 # Defenseman-only field (see fetch_blocked_shots_by_season) -
                 # left at 0 for forwards, who never get a real value merged
                 # in; the page only displays this column for defensemen, so
                 # a forward's 0 here is never shown as if it meant something.
                 "blocked_shots": 0}

PROJECTION_FORMULA = (
    "Projected points this week = blended points/game x games this week. "
    "Blend weight on this season = min(games played this season / 20, 1); "
    "the rest of the weight is points/game pooled across the two prior "
    f"seasons combined ({PRIOR_SEASON_LABELS[PRIOR_SEASONS[0]]} + "
    f"{PRIOR_SEASON_LABELS[PRIOR_SEASONS[1]]}), not last season alone, so a "
    "small sample in one season can't dominate the blend - that pooled rate "
    f"requires at least {MIN_POOLED_GAMES} games across those two seasons "
    "combined; below that it's blank, same as a player with no NHL history. "
    "Early in the season the projection leans on the pooled prior rate; by "
    "20 games played this season it is entirely this season's rate."
)


def atomic_write_json(path, data, indent=2):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_previous_output():
    """The last successfully-written player_hub.json, used two ways: to
    backfill a team whose roster fetch failed this run (see
    carry_over_failed_teams) and as the baseline for sanity_check_or_abort's
    "did this run lose most of its data" guard. None on a first-ever run,
    or if the existing file is somehow unreadable - both treated as
    "nothing to compare against or borrow from," not an error."""
    if not os.path.exists(OUTPUT_PATH):
        return None
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  could not read previous {OUTPUT_PATH} for comparison: {e}")
        return None


def carry_over_failed_teams(failed_teams, previous, players_out, goalies_out):
    """For every team whose roster fetch failed after retries this run,
    copy that team's players/goalies from the previous file verbatim
    (rather than leaving them missing) - a team briefly unreachable is
    not the same as a team with no players, and stale-but-real data beats
    no data for the "blanks, not drops" rule this file follows elsewhere.
    Entries carried this way keep whatever "generated" values they had
    (there are none per-player) and are listed in the "stale_teams" field
    of the output so staleness is visible, not silent."""
    if not failed_teams or not previous:
        return
    prev_players = previous.get("players", {})
    prev_goalies = previous.get("goalies", {})
    carried_players = 0
    carried_goalies = 0
    for pid, p in prev_players.items():
        if p.get("team") in failed_teams and pid not in players_out:
            players_out[pid] = p
            carried_players += 1
    for pid, g in prev_goalies.items():
        if g.get("team") in failed_teams and pid not in goalies_out:
            goalies_out[pid] = g
            carried_goalies += 1
    print(f"  Carried over {carried_players} skaters and {carried_goalies} goalies from the "
          f"previous file for team(s) whose roster fetch failed: {', '.join(sorted(failed_teams))}")


def sanity_check_or_abort(previous, players_out, goalies_out, threshold=0.8):
    """Refuses to write a file with substantially fewer players/goalies
    than the previous one - the backstop for any failure mode this run
    didn't already catch and recover from (carry_over_failed_teams
    handles the specific "team fetch failed" case; this catches anything
    else that could gut the file, known or not). threshold=0.8 means
    "abort if the new total is more than 20% below the old total."
    A missing or empty previous file means there's nothing to compare
    against - a first run is allowed through regardless of size."""
    if not previous:
        return
    old_total = len(previous.get("players", {})) + len(previous.get("goalies", {}))
    if old_total == 0:
        return
    new_total = len(players_out) + len(goalies_out)
    if new_total < old_total * threshold:
        pct = new_total / old_total
        print(f"\nABORTING: new file would have {new_total} players/goalies vs. the previous "
              f"{old_total} ({pct:.0%}) - more than {int((1 - threshold) * 100)}% fewer. "
              "Keeping the previous file on disk instead of committing a gutted one.")
        sys.exit(1)


def cache_entry_has_goalie_fields(entry):
    """True if every PRIOR_SEASONS row in this cache entry already has
    the goalie fields (wins, as a sentinel key). A cache entry written
    before goalie support was added won't have them - re-fetching those
    (rather than silently reading 0 for a real career total) is a
    one-time cost, same as any other "uncached" player on a fresh run."""
    return all("wins" in entry.get(sid, {}) for sid in PRIOR_SEASONS)


def week_bounds(anchor_date):
    """Monday-Sunday of the calendar week containing anchor_date - same
    definition build_schedule_analysis.py's week_start() uses."""
    d = anchor_date.date() if hasattr(anchor_date, "date") else anchor_date
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def fetch_team_summary(season):
    """{abbrev: {...}} for every team that has played at least one game
    in this season - one call for all 32 teams, not 32 calls."""
    url = "https://api.nhle.com/stats/rest/en/team/summary"
    params = {"cayenneExp": f"seasonId={season} and gameTypeId=2"}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        rows = r.json().get("data", [])
    except Exception as e:
        print(f"  team summary error for season {season}: {e}")
        return {}
    full_to_abbrev = {v: k for k, v in FULL_NAMES.items()}
    out = {}
    for row in rows:
        abbrev = full_to_abbrev.get(row.get("teamFullName"))
        if not abbrev:
            continue
        out[abbrev] = {
            "goals_for_per_game": round(row.get("goalsForPerGame", 0), 2),
            "goals_against_per_game": round(row.get("goalsAgainstPerGame", 0), 2),
            "power_play_pct": round(row.get("powerPlayPct", 0), 3),
            "penalty_kill_pct": round(row.get("penaltyKillPct", 0), 3),
            "games_played": row.get("gamesPlayed", 0),
        }
    return out


def fetch_team_schedule(team):
    """[] on total failure (retried with backoff first) - a missing
    schedule already degrades gracefully downstream (games_this_week
    falls to 0 for that team, never a dropped player), unlike a missing
    roster, which erases identity entirely. See roster_stats.py's
    docstring for why that distinction matters and why the roster fetch
    specifically must never make the same {}-on-failure substitution."""
    data = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/club-schedule-season/{team}/{SEASON}",
        label=f"schedule/{team}",
    )
    if data is None:
        return []
    games = data.get("games", [])
    games.sort(key=lambda g: g["gameDate"])
    return games


def fetch_team_club_stats(team):
    data = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/club-stats/{team}/now",
        label=f"club-stats/{team}",
    )
    return data if data is not None else {}


def current_season_stats(club_stats):
    """{playerId: EMPTY_SEASON-shaped dict} for skaters AND goalies from
    this team's club-stats/now payload - strictly THIS season's real
    numbers, zeroed out entirely (not last season's numbers relabeled)
    if club-stats/now's own season field isn't actually SEASON yet."""
    out = {}
    is_live = str(club_stats.get("season")) == SEASON
    for s in club_stats.get("skaters", []):
        pid = s.get("playerId")
        if not is_live:
            out[pid] = dict(EMPTY_SEASON)
            continue
        gp = s.get("gamesPlayed", 0)
        out[pid] = {
            "games_played": gp, "points": s.get("points", 0), "goals": s.get("goals", 0),
            "assists": s.get("assists", 0), "shots": s.get("shots", 0),
            "shots_against": 0, "goals_against": 0,
        }
    for g in club_stats.get("goalies", []):
        pid = g.get("playerId")
        if not is_live:
            out[pid] = dict(EMPTY_SEASON)
            continue
        out[pid] = {
            "games_played": g.get("gamesPlayed", 0), "points": 0, "goals": 0, "assists": 0, "shots": 0,
            "shots_against": g.get("shotsAgainst", 0), "goals_against": g.get("goalsAgainst", 0),
            "games_started": g.get("gamesStarted", 0), "wins": g.get("wins", 0),
            "losses": g.get("losses", 0), "ot_losses": g.get("overtimeLosses", 0),
            "shutouts": g.get("shutouts", 0), "toi_seconds": g.get("timeOnIce", 0),
        }
    return out


def parse_toi_seconds(s):
    """'MM:SS' (or 'M:SS') -> total seconds. Used for both a single
    boxscore's TOI and a landing page season row's cumulative TOI - same
    format either way."""
    try:
        m, sec = s.split(":")
        return int(m) * 60 + int(sec)
    except Exception:
        return 0


def get_starter_id(game_id, team):
    """Starting goalie's playerId for `team` in this game, falling back
    to whichever goalie logged the most TOI if neither is flagged
    starter (confirmed live this happens in preseason boxscores)."""
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore", timeout=15)
        r.raise_for_status()
        box = r.json()
    except Exception as e:
        print(f"  boxscore error for game {game_id}: {e}")
        return None

    pbs = box.get("playerByGameStats", {})
    away_abbrev = box.get("awayTeam", {}).get("abbrev")
    side = "awayTeam" if away_abbrev == team else "homeTeam"
    goalies = pbs.get(side, {}).get("goalies", [])
    if not goalies:
        return None

    starter = next((g for g in goalies if g.get("starter") is True), None)
    if starter is None:
        starter = max(goalies, key=lambda g: parse_toi_seconds(g.get("toi", "0:00")))
    return starter.get("playerId")


def fetch_goalie_last_start(player_id):
    """The date THIS specific goalie last started (gamesStarted flag on
    their own game log), not just their team's most recent game - a
    backup who hasn't played in weeks must not inherit the starter's
    date. One call per goalie instead of walking every team's boxscores
    backward game-by-game to find each goalie's own last appearance.
    Checks this season's regular-season log first, falling back to this
    season's preseason log if empty (the only signal available before
    the regular season starts - same reasoning as
    most_recent_completed_game's "preseason counts" rule elsewhere in
    this file). Never falls back to a PRIOR season - a goalie's last
    start from a season ago isn't a "who's playing now" signal once the
    season has turned over.

    Confirmed live: this endpoint can lag a day or more behind the
    score/boxscore endpoints for the very newest preseason games (a
    goalie who started the very first preseason game of 2026-27 still
    returned an empty game-log the next day) - not a bug here, just this
    specific NHL API endpoint settling later than others early in a new
    season. Returns None rather than falling back to a boxscore walk for
    that gap, consistent with this file's "blank beats a guess" rule."""
    for game_type in (2, 1):
        try:
            r = requests.get(
                f"https://api-web.nhle.com/v1/player/{player_id}/game-log/{SEASON}/{game_type}",
                timeout=15,
            )
            r.raise_for_status()
            games = r.json().get("gameLog", [])
        except Exception as e:
            print(f"    game-log error for player {player_id} (type {game_type}): {e}")
            continue
        starts = [g for g in games if g.get("gamesStarted")]
        if starts:
            return starts[0]["gameDate"]  # game-log is sorted most-recent-first
    return None


def most_recent_completed_game(schedule, today_str):
    """Most recent completed game of ANY type as of today - preseason
    counts here, since it's the only signal available before the
    regular season starts."""
    past = [g for g in schedule if g["gameDate"] < today_str and g.get("gameState") in ("OFF", "FINAL")]
    return past[-1] if past else None


def fetch_player_landing(player_id):
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/player/{player_id}/landing", timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"    landing error for player {player_id}: {e}")
        return None


def extract_prior_seasons(landing):
    """{season_id: EMPTY_SEASON-shaped dict} for PRIOR_SEASONS only -
    summing multiple rows for the same season (a mid-season trade
    produces one NHL regular-season row per team - confirmed live).
    Goalie-only counting fields (games_started, wins, losses, ot_losses,
    shutouts, toi_seconds) are pooled the same way for a traded goalie -
    a skater row simply doesn't have these keys, so they default to 0
    and are never read for a skater identity anyway."""
    out = {}
    for season_id in PRIOR_SEASONS:
        rows = [
            s for s in (landing.get("seasonTotals") or [])
            if s.get("leagueAbbrev") == "NHL" and s.get("gameTypeId") == 2
            and str(s.get("season")) == season_id
        ]
        out[season_id] = {
            "games_played": sum(r.get("gamesPlayed", 0) for r in rows),
            "points": sum(r.get("points", 0) for r in rows),
            "goals": sum(r.get("goals", 0) for r in rows),
            "assists": sum(r.get("assists", 0) for r in rows),
            "shots": sum(r.get("shots", 0) for r in rows),
            "shots_against": sum(r.get("shotsAgainst", 0) for r in rows),
            "goals_against": sum(r.get("goalsAgainst", 0) for r in rows),
            "games_started": sum(r.get("gamesStarted", 0) for r in rows),
            "wins": sum(r.get("wins", 0) for r in rows),
            "losses": sum(r.get("losses", 0) for r in rows),
            "ot_losses": sum(r.get("otLosses", 0) for r in rows),
            "shutouts": sum(r.get("shutouts", 0) for r in rows),
            "toi_seconds": sum(parse_toi_seconds(r.get("timeOnIce", "0:00")) for r in rows),
        }
    return out


def fetch_blocked_shots_by_season(season):
    """{playerId: total blocked shots} for every defenseman in this
    season - api.nhle.com/stats/rest/en/skater/realtime, filtered
    server-side to positionCode='D' (so we're not paying to page through
    forward rows we'd discard) and paginated 100 rows per call, the
    server's apparent hard cap regardless of a larger requested limit
    (confirmed live: limit=1000 still returned 100 rows). Neither
    club-stats/now nor player/{id}/landing carries blocked shots at all
    for skaters - confirmed live by inspecting both directly - this
    separate bulk stats-REST endpoint is the only place it exists. Not
    folded into the per-player prior-seasons cache: unlike that cache,
    refetching this every run costs almost nothing (a handful of calls
    total across all three seasons combined, see this function's
    call-site in main() for the exact count), so there's no benefit to
    the added complexity of caching it."""
    out = {}
    start = 0
    while True:
        try:
            r = requests.get(
                "https://api.nhle.com/stats/rest/en/skater/realtime",
                params={"cayenneExp": f"seasonId={season} and gameTypeId=2 and positionCode='D'",
                        "limit": 100, "start": start},
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            print(f"  blocked-shots error for season {season} (start {start}): {e}")
            break
        rows = payload.get("data", [])
        for row in rows:
            out[row.get("playerId")] = row.get("blockedShots", 0)
        total = payload.get("total", 0)
        start += len(rows)
        if not rows or start >= total:
            break
    return out


def rate_stats(raw):
    """EMPTY_SEASON-shaped raw counts -> the display shape (label filled
    in by the caller), with points_per_game/shots_per_game/save_pct null
    (never 0) when games_played is 0.

    blocked_shots reads via .get(), not raw["blocked_shots"], because
    this same function is also called with raw cache entries that
    predate that field (any dict written before blocked-shots support
    existed) - safe specifically because the only two callers are the
    last-starting-goalie lookup (never a defenseman, a 0 here is never
    displayed) and the main skater loop, which always merges a freshly
    bulk-fetched real value in for a defenseman BEFORE calling this - so
    this default never masks a real defenseman's actual total."""
    gp = raw["games_played"]
    save_pct = None
    if raw["shots_against"] > 0:
        save_pct = round((raw["shots_against"] - raw["goals_against"]) / raw["shots_against"], 3)
    return {
        "games_played": gp, "points": raw["points"], "goals": raw["goals"], "assists": raw["assists"],
        "shots": raw["shots"], "blocked_shots": raw.get("blocked_shots", 0),
        "points_per_game": round(raw["points"] / gp, 3) if gp else None,
        "shots_per_game": round(raw["shots"] / gp, 3) if gp else None,
        "save_pct": save_pct,
    }


def rate_stats_goalie(raw):
    """EMPTY_SEASON-shaped raw counts -> a goalie's display shape. No
    projection, no derived "value" number - just the real counting
    stats plus two derived rates that ARE honestly computable from raw
    counts: save_pct from shots_against/goals_against (identical
    definition to rate_stats' save_pct), and GAA from goals_against and
    total TOI (goals per 60 minutes - NOT goals_against/games_played,
    which would be a different and less standard number). Both are null,
    never 0, when there's no relevant playing time to compute them from -
    same "blank means no data" rule as everywhere else in this file."""
    gaa = None
    if raw["toi_seconds"] > 0:
        gaa = round(raw["goals_against"] * 3600 / raw["toi_seconds"], 2)
    save_pct = None
    if raw["shots_against"] > 0:
        save_pct = round((raw["shots_against"] - raw["goals_against"]) / raw["shots_against"], 3)
    return {
        "games_played": raw["games_played"],
        "games_started": raw["games_started"],
        "wins": raw["wins"],
        "losses": raw["losses"],
        "ot_losses": raw["ot_losses"],
        "shutouts": raw["shutouts"],
        "save_pct": save_pct,
        "gaa": gaa,
    }


def project_points(this_raw, prior_raw_list):
    """prior_raw_list: one EMPTY_SEASON-shaped raw dict per PRIOR_SEASONS
    entry, POOLED together (games and points summed across both prior
    seasons) rather than using the single most recent prior season alone
    - confirmed live that a one-game flare-up (Oliver Bonk: 1 GP, 2 Pts
    in 2025-26 alone, a 2.00 PPG that out-projected McDavid) can otherwise
    dominate the blend. The pooled rate requires at least MIN_POOLED_GAMES
    games across the two prior seasons combined; below that it's null
    (blank on the page), the same treatment a true rookie with zero prior
    games already gets - a few-game sample isn't meaningfully different
    from no sample.

    Returns (blended_ppg, weight_this_season, prior_ppg, prior_games_pooled).
    """
    this_rate = rate_stats(this_raw)
    gp_this = this_raw["games_played"]
    ppg_this = this_rate["points_per_game"]

    prior_gp = sum(r["games_played"] for r in prior_raw_list)
    prior_pts = sum(r["points"] for r in prior_raw_list)
    ppg_prior = round(prior_pts / prior_gp, 3) if prior_gp >= MIN_POOLED_GAMES else None

    if ppg_this is None and ppg_prior is None:
        return None, 0.0, ppg_prior, prior_gp

    weight_this = min(gp_this / 20, 1)
    if ppg_this is None:
        blended = ppg_prior
        weight_this = 0.0
    elif ppg_prior is None:
        blended = ppg_this
        weight_this = 1.0
    else:
        blended = weight_this * ppg_this + (1 - weight_this) * ppg_prior
    return round(blended, 3), round(weight_this, 3), ppg_prior, prior_gp


def build_team_context(team, team_stats_current, team_stats_last, schedule, week_start, week_end,
                        last_start_cache):
    stats = team_stats_current.get(team)
    stat_season = "current"
    if not stats or stats.get("games_played", 0) == 0:
        stats = team_stats_last.get(team)
        stat_season = "last_season"
    if not stats:
        stats = {"goals_for_per_game": None, "goals_against_per_game": None,
                  "power_play_pct": None, "penalty_kill_pct": None}
        stat_season = None

    week_games = []
    for g in schedule:
        if g.get("gameType") != 2:
            continue
        if not (week_start <= g["gameDate"] <= week_end):
            continue
        opponent = g["awayTeam"]["abbrev"] if g["homeTeam"]["abbrev"] == team else g["homeTeam"]["abbrev"]
        home = g["homeTeam"]["abbrev"] == team

        opp_stats = team_stats_current.get(opponent)
        opp_stat_season = "current"
        if not opp_stats or opp_stats.get("games_played", 0) == 0:
            opp_stats = team_stats_last.get(opponent)
            opp_stat_season = "last_season"

        entry = {
            "date": g["gameDate"], "opponent": opponent, "home": home,
            "opponent_goals_against_per_game": opp_stats.get("goals_against_per_game") if opp_stats else None,
            "opponent_penalty_kill_pct": opp_stats.get("penalty_kill_pct") if opp_stats else None,
            "opponent_stat_season": opp_stat_season if opp_stats else None,
            "last_start": last_start_cache.get(opponent),
        }
        week_games.append(entry)

    return {
        "full_name": FULL_NAMES.get(team, team),
        "goals_for_per_game": stats.get("goals_for_per_game"),
        "power_play_pct": stats.get("power_play_pct"),
        "team_stat_season": stat_season,
        "week_games": week_games,
    }


def main():
    print(f"Building player hub for {SEASON_LABEL}...")
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    is_preseason = today_str < SEASON_START
    anchor = datetime.strptime(SEASON_START, "%Y-%m-%d") if is_preseason else today
    week_start, week_end = week_bounds(anchor)
    print(f"Preseason: {is_preseason}. Week shown: {week_start} to {week_end}"
          + (" (season-opening week - today's calendar week has no real games yet)" if is_preseason else ""))

    cache = load_cache()
    print(f"Prior-season cache loaded: {len(cache)} players already cached")

    previous_output = load_previous_output()

    print("Fetching team summary stats (current + last season, one call each)...")
    team_stats_current = fetch_team_summary(SEASON)
    team_stats_last = fetch_team_summary(PRIOR_SEASONS[0])
    print(f"  current season: {len(team_stats_current)} teams with games played")
    print(f"  last season: {len(team_stats_last)} teams")

    print("Fetching defenseman blocked shots (this season + both prior, bulk per season)...")
    blocked_shots_by_season = {}
    for sid in [SEASON] + PRIOR_SEASONS:
        blocked_shots_by_season[sid] = fetch_blocked_shots_by_season(sid)
        print(f"  {sid}: {len(blocked_shots_by_season[sid])} defensemen with a blocked-shots total")

    teams = sorted(FULL_NAMES)
    schedules = {}
    rosters = {}
    club_stats = {}
    current_by_team = {}
    last_start_cache = {}
    failed_roster_teams = []  # teams whose roster fetch failed after retries - see below

    print(f"Fetching schedules + rosters + current club-stats for {len(teams)} teams...")
    for team in teams:
        schedules[team] = fetch_team_schedule(team)
        roster = get_current_roster(team)
        if roster is None:
            # Every retry inside get_current_roster() already failed - this
            # is a confirmed-unreachable team, not a team with zero
            # players (no NHL team has zero rostered players). Recording
            # {} here keeps every downstream loop crash-free, but the team
            # stays in failed_roster_teams so main() can backfill its
            # players from the previous file instead of silently letting
            # them vanish - see roster_stats.py's docstring for the
            # incident this replaced.
            failed_roster_teams.append(team)
            roster = {}
        rosters[team] = roster
        club_stats[team] = fetch_team_club_stats(team)
        current_by_team[team] = current_season_stats(club_stats[team])
        # A conservative gap between teams, on top of fetch_json_with_retry's
        # own backoff on a failed request - confirmed live that the NHL API
        # throttled this exact loop after ~8 teams' worth of requests in a
        # 2026-09-25 run, so slowing the steady-state pace (not just
        # reacting after a failure) is part of the fix, not just retrying.
        time.sleep(0.6)

    if failed_roster_teams:
        print(f"  WARNING: roster fetch failed after retries for {len(failed_roster_teams)} "
              f"team(s), even with backoff: {', '.join(failed_roster_teams)}")

    print("Finding each team's last starting goalie (most recent completed game)...")
    for team in teams:
        game = most_recent_completed_game(schedules[team], today_str)
        if not game:
            last_start_cache[team] = None
            continue
        starter_id = get_starter_id(game["id"], team)
        if not starter_id:
            last_start_cache[team] = None
            continue
        identity = rosters[team].get(starter_id)
        pid_str = str(starter_id)
        if pid_str not in cache:
            landing = fetch_player_landing(starter_id)
            time.sleep(0.15)
            if landing:
                cache[pid_str] = extract_prior_seasons(landing)
        prior = cache.get(pid_str, {s: dict(EMPTY_SEASON) for s in PRIOR_SEASONS})
        current_raw = current_by_team[team].get(starter_id, dict(EMPTY_SEASON))
        rate = rate_stats(current_raw)
        stat_season = "current"
        if rate["save_pct"] is None or current_raw["games_played"] == 0:
            rate = rate_stats(prior[PRIOR_SEASONS[0]])
            stat_season = "last_season"
        name = f"{identity['first_name']} {identity['last_name']}".strip() if identity else "Unknown"
        last_start_cache[team] = {
            "player_id": starter_id, "name": name.strip(),
            "sv_pct": rate["save_pct"], "stat_season": stat_season if rate["save_pct"] is not None else None,
            "date": game["gameDate"],
        }
        print(f"  {team}: last start {name.strip()} on {game['gameDate']} "
              f"({rate['save_pct'] if rate['save_pct'] is not None else '—'} SV%, {stat_season})")

    print("Building team context...")
    teams_out = {}
    for team in teams:
        teams_out[team] = build_team_context(
            team, team_stats_current, team_stats_last, schedules[team],
            week_start, week_end, last_start_cache
        )

    print("Building every rostered skater's 3-season stats...")
    players_out = {}
    total_skaters = sum(
        1 for team in teams for identity in rosters[team].values() if identity["group"] != "goalies"
    )
    print(f"  {total_skaters} rostered skaters across {len(teams)} teams")

    done = 0
    landing_calls = 0
    for team in teams:
        games_this_week = len(teams_out[team]["week_games"])
        for player_id, identity in rosters[team].items():
            if identity["group"] == "goalies":
                continue
            pid_str = str(player_id)
            if pid_str not in cache:
                landing = fetch_player_landing(player_id)
                time.sleep(0.15)
                landing_calls += 1
                if landing:
                    cache[pid_str] = extract_prior_seasons(landing)
            done += 1
            if done % 100 == 0:
                print(f"  Progress: {done}/{total_skaters} ({landing_calls} landing-page calls so far)")

            prior = cache.get(pid_str, {s: dict(EMPTY_SEASON) for s in PRIOR_SEASONS})
            current_raw = dict(current_by_team[team].get(player_id, dict(EMPTY_SEASON)))

            is_defenseman = identity["group"] == "defensemen"
            if is_defenseman:
                current_raw["blocked_shots"] = blocked_shots_by_season[SEASON].get(player_id, 0)

            seasons = {SEASON: {**rate_stats(current_raw), "label": SEASON_LABEL}}
            for sid in PRIOR_SEASONS:
                # Copy before mutating - prior[sid] is a reference into the
                # shared cache dict, which must never carry blocked_shots
                # (that field is bulk-refetched every run, not cached).
                prior_raw = dict(prior[sid])
                if is_defenseman:
                    prior_raw["blocked_shots"] = blocked_shots_by_season[sid].get(player_id, 0)
                seasons[sid] = {**rate_stats(prior_raw), "label": ALL_SEASON_LABELS[sid]}
            for sid in seasons:
                seasons[sid].pop("save_pct", None)

            prior_raw_list = [prior[sid] for sid in PRIOR_SEASONS]
            blended_ppg, weight_this, prior_ppg, prior_gp_pooled = project_points(current_raw, prior_raw_list)
            projected = round(blended_ppg * games_this_week, 2) if blended_ppg is not None else None

            players_out[pid_str] = {
                "name": f"{identity['first_name']} {identity['last_name']}".strip(),
                "team": team,
                "position": identity["position"],
                "seasons": seasons,
                "projection": {
                    "games_this_week": games_this_week,
                    "weight_this_season": weight_this,
                    "prior_points_per_game": prior_ppg,
                    "prior_games_pooled": prior_gp_pooled,
                    "blended_points_per_game": blended_ppg,
                    "projected_points": projected,
                },
            }

    print(f"Landing-page calls this run: {landing_calls} (of {total_skaters} skaters - the rest served from cache)")

    print("Building every rostered goalie's 3-season stats...")
    goalies_out = {}
    total_goalies = sum(
        1 for team in teams for identity in rosters[team].values() if identity["group"] == "goalies"
    )
    print(f"  {total_goalies} rostered goalies across {len(teams)} teams")

    done_g = 0
    goalie_landing_calls = 0
    goalie_gamelog_calls = 0
    for team in teams:
        games_this_week = len(teams_out[team]["week_games"])
        for player_id, identity in rosters[team].items():
            if identity["group"] != "goalies":
                continue
            pid_str = str(player_id)
            if pid_str not in cache or not cache_entry_has_goalie_fields(cache[pid_str]):
                landing = fetch_player_landing(player_id)
                time.sleep(0.15)
                goalie_landing_calls += 1
                if landing:
                    cache[pid_str] = extract_prior_seasons(landing)
            done_g += 1

            prior = cache.get(pid_str, {s: dict(EMPTY_SEASON) for s in PRIOR_SEASONS})
            current_raw = current_by_team[team].get(player_id, dict(EMPTY_SEASON))

            seasons = {SEASON: {**rate_stats_goalie(current_raw), "label": SEASON_LABEL}}
            for sid in PRIOR_SEASONS:
                seasons[sid] = {**rate_stats_goalie(prior[sid]), "label": ALL_SEASON_LABELS[sid]}

            last_start_date = fetch_goalie_last_start(player_id)
            time.sleep(0.15)
            goalie_gamelog_calls += 1

            goalies_out[pid_str] = {
                "name": f"{identity['first_name']} {identity['last_name']}".strip(),
                "team": team,
                "position": identity["position"],
                "seasons": seasons,
                "games_this_week": games_this_week,
                "last_start_date": last_start_date,
            }

    print(f"Goalie landing-page calls this run: {goalie_landing_calls} (of {total_goalies} goalies - "
          f"the rest served from cache), {goalie_gamelog_calls} game-log calls for last-start dates")

    save_cache(cache)

    carry_over_failed_teams(failed_roster_teams, previous_output, players_out, goalies_out)
    sanity_check_or_abort(previous_output, players_out, goalies_out)

    output = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "season": SEASON,
        "season_label": SEASON_LABEL,
        "prior_seasons": PRIOR_SEASONS,
        "preseason": is_preseason,
        "season_start": SEASON_START,
        "week_start": week_start,
        "week_end": week_end,
        "projection_formula": PROJECTION_FORMULA,
        "stale_teams": sorted(failed_roster_teams),
        "teams": teams_out,
        "players": players_out,
        "goalies": goalies_out,
    }

    os.makedirs("data", exist_ok=True)
    atomic_write_json(OUTPUT_PATH, output)

    print(f"\n{'='*60}")
    print(f"Wrote {len(players_out)} skaters and {len(goalies_out)} goalies across {len(teams_out)} teams to {OUTPUT_PATH}"
          + (f" ({len(failed_roster_teams)} team(s) stale from a failed fetch)" if failed_roster_teams else ""))


def save_cache(cache):
    atomic_write_json(CACHE_PATH, cache)


if __name__ == "__main__":
    main()
