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
"""
import requests


def get_current_roster(team):
    """{playerId: {first_name, last_name, position, group}} for every
    player actually on this team's roster right now."""
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/roster/{team}/current", timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  roster error for {team}: {e}")
        return {}
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
    the last completed season's."""
    try:
        r = requests.get(f"https://api-web.nhle.com/v1/club-stats/{team}/now", timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  club-stats error for {team}: {e}")
        return {}


def roster_with_stats(team, current_season):
    """Joins the current roster against club-stats/{team}/now by
    playerId. Returns {playerId: {first_name, last_name, position,
    group, stats: <club-stats player dict or None>, stat_season:
    "current"/"last_season"/None}}.

    current_season is the caller's own "YYYYYYYY" season string (e.g.
    "20262027") - compared against club-stats' own season field, never
    assumed. stat_season is None only when stats is also None (nothing
    to label).
    """
    roster = get_current_roster(team)
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
