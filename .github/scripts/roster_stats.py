#!/usr/bin/env python3
"""
Single source of truth for "who is actually on an NHL team's roster
right now, with real season-to-date stats attached" - the fix for a
confirmed data bug where club-stats/{team}/now was used directly as if
it were the current roster. It isn't: that endpoint returns every player
who has accumulated stats for that team, including anyone since traded,
waived, or let go as a free agent. Confirmed concretely: Darnell Nurse
(no longer on Edmonton as of the 2026 offseason) still appears in
club-stats/EDM/now with a full 82-game 2025-26 stat line, and was
rendered as a "current usage leader" for Edmonton before this fix.

roster/{team}/current is the authoritative source for team membership.
club-stats/{team}/now is joined onto it by playerId - not by name, which
risks silent false matches or misses on suffixes, accents, and common
surnames - and only players who are actually on the current roster
survive the join. A roster player with no matching stats entry (a new
signing who hasn't played for this team yet, or a rookie) is left with
stats=None: blank, never a substituted or estimated number.

club-stats/{team}/now also has no live in-season data until real games
have been played this season - it carries its own "season" field
("20252026", etc.), which this module compares against the caller's
current-season string to tag every result stat_season: "current" or
"last_season". Callers MUST surface that label wherever a stat is
displayed or fed into a prompt - a last-season number must never be
presented as this season's form.

RETRY/BACKOFF AND THE "MISSING vs EMPTY" DISTINCTION (added after a
confirmed real incident): a run of build_player_hub.py on 2026-09-25
fetched roster/{team}/current successfully for the first 8 teams
(alphabetically) and then got EVERY SINGLE remaining team's roster
fetch back empty - consistent with the NHL API throttling the run after
a burst of requests, not 24 unrelated coincidental failures. Because
get_current_roster() used to catch that failure and return {} - the
exact same shape a team with a genuinely empty roster would produce -
build_player_hub.py had no way to tell "this team has no players" apart
from "we couldn't reach the API for this team," and silently wrote 24
teams' worth of real, rostered players (750 of them, confirmed by
re-fetching live) out of the file entirely.

fetch_json_with_retry() below is the fix at the transport level: retry
with exponential backoff (plus jitter) on anything transient - a
connection error, a timeout, HTTP 429, or a 5xx - and return None
(never {} or []) only once every retry is exhausted. get_current_roster()
propagates that None rather than converting it to {}, so a caller can
tell real emptiness from a failed fetch and react accordingly (see
build_player_hub.py's team-level fallback to the previous file, and its
pre-write sanity check, for how that distinction gets used).

get_club_stats() keeps degrading to {} on total failure rather than
None - a missing stats payload for an otherwise-real roster already
degrades gracefully everywhere in this file (stats=None, "no stats
entry" - never an invented number), so there is no equivalent
identity-loss risk there the way there is for the roster fetch itself.
"""
import random
import time

import requests

DEFAULT_TIMEOUT = 15
DEFAULT_MAX_RETRIES = 4
DEFAULT_BASE_DELAY = 1.5  # seconds; doubles each retry, plus up to 0.5s jitter


def fetch_with_retry(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT,
                      max_retries=DEFAULT_MAX_RETRIES, base_delay=DEFAULT_BASE_DELAY,
                      label=None):
    """GET url and return the raw Response, or None if every attempt
    fails. This is the shared primitive - fetch_json_with_retry() below
    is just this plus .json() for a JSON API; a caller scraping HTML or
    reading any other body format (e.g. fetch_injuries.py's NHL.com team
    pages) uses this directly instead.

    Retries (with exponential backoff + jitter) on a connection error, a
    timeout, HTTP 429, or a 5xx - all things a rate limit or a transient
    outage produces and a retry can plausibly recover from. A 4xx other
    than 429 is treated as permanent (a bad team code, a dead endpoint)
    and fails immediately without burning the retry budget on an error
    that will never resolve itself.

    None is the ONLY return value on total failure - never {} or [] -
    so a caller can always distinguish "the source told us this is
    empty" from "we couldn't reach the source," which get_current_roster()
    below depends on. Every fetch function in this repo's scripts should
    use this (or fetch_json_with_retry) instead of a bare requests.get()
    + try/except that swallows the difference.
    """
    tag = label or url
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if attempt == max_retries:
                print(f"  {tag}: failed after {max_retries} attempts ({e})")
                return None
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  {tag}: attempt {attempt} error ({e}) - retrying in {delay:.1f}s")
            time.sleep(delay)
            continue

        if r.status_code == 429 or r.status_code >= 500:
            if attempt == max_retries:
                print(f"  {tag}: failed after {max_retries} attempts (HTTP {r.status_code})")
                return None
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"  {tag}: HTTP {r.status_code} on attempt {attempt} - retrying in {delay:.1f}s")
            time.sleep(delay)
            continue

        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            print(f"  {tag}: permanent error, not retrying ({e})")
            return None

        return r
    return None


def fetch_json_with_retry(url, params=None, timeout=DEFAULT_TIMEOUT,
                           max_retries=DEFAULT_MAX_RETRIES, base_delay=DEFAULT_BASE_DELAY,
                           label=None):
    """fetch_with_retry() plus .json() decoding - see that function's
    docstring for the retry/backoff and None-on-failure behavior."""
    r = fetch_with_retry(url, params=params, timeout=timeout, max_retries=max_retries,
                          base_delay=base_delay, label=label)
    return r.json() if r is not None else None


def get_current_roster(team):
    """{playerId: {first_name, last_name, position, group}} for every
    player actually on this team's roster right now, or None if the
    fetch failed after retries - callers must not treat None as "empty
    roster" (see this module's docstring for the incident that made this
    distinction necessary)."""
    data = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/roster/{team}/current",
        label=f"roster/{team}/current",
    )
    if data is None:
        return None
    players = {}
    for group in ("forwards", "defensemen", "goalies"):
        for p in data.get(group, []):
            players[p["id"]] = {
                "first_name": p.get("firstName", {}).get("default", ""),
                "last_name": p.get("lastName", {}).get("default", ""),
                "position": p.get("positionCode", ""),
                "group": group,
            }
    return players


def get_club_stats(team):
    """Raw club-stats/{team}/now payload - includes its own "season"
    field, needed to determine whether returned numbers are current or
    the last completed season's. Degrades to {} on total failure (not
    None) - a missing stats payload already degrades gracefully
    everywhere it's read (stats=None per player, never invented), unlike
    a missing roster which erases player identity entirely."""
    data = fetch_json_with_retry(
        f"https://api-web.nhle.com/v1/club-stats/{team}/now",
        label=f"club-stats/{team}/now",
    )
    return data if data is not None else {}


def roster_with_stats(team, current_season):
    """Joins the current roster against club-stats/{team}/now by
    playerId. Returns {playerId: {first_name, last_name, position,
    group, stats: <club-stats player dict or None>, stat_season:
    "current"/"last_season"/None}}, or None if the roster fetch itself
    failed after retries - propagated from get_current_roster() so a
    caller can tell "this team's roster is genuinely unavailable right
    now" apart from "this team has zero rostered players" (never true
    for a real NHL team) and react accordingly instead of silently
    treating the two the same.

    current_season is the caller's own "YYYYYYYY" season string (e.g.
    "20262027") - compared against club-stats' own season field, never
    assumed. stat_season is None only when stats is also None (nothing
    to label).
    """
    roster = get_current_roster(team)
    if roster is None:
        return None
    stats_payload = get_club_stats(team)

    stat_season = None
    if stats_payload.get("season") is not None:
        stat_season = "current" if str(stats_payload["season"]) == str(current_season) else "last_season"

    stat_by_id = {}
    for group in ("skaters", "goalies"):
        for p in stats_payload.get(group, []):
            stat_by_id[p.get("playerId")] = p

    out = {}
    for pid, identity in roster.items():
        s = stat_by_id.get(pid)
        out[pid] = {**identity, "stats": s, "stat_season": stat_season if s else None}
    return out
