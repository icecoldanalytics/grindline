#!/usr/bin/env python3
"""
Automates data/scratches.json using the NHL's own official injury
report pages (one per team, e.g. nhl.com/blackhawks/team/injury-report)
instead of relying on manual updates.

KNOWN LIMITATION (as of this writing, July 2026 — off-season): every
team's report currently shows "no current injured players" since the
season is over. This script has been tested against that EMPTY state
successfully, but has NOT been verified against a real, populated
injury list yet, since none exist right now. Re-check this closely
once the 2026-27 season starts and some teams have actual injuries
listed — the table-parsing logic may need adjustment if the page
structure looks any different once populated.

RETRY/BACKOFF AND STALE-TEAM FALLBACK: same treatment as the confirmed
build_player_hub.py bug this repo already fixed once (see
roster_stats.py's docstring) - fetch_with_retry() retries a failed
team's page with backoff before giving up, a team that still fails gets
its previous run's injuries carried over rather than silently cleared,
and the whole run aborts (keeping the previous file) if more than 20%
of the 32 teams failed even after retries, rather than publishing a
scratch list built mostly from stale fallbacks.
"""
import json
import os
import re
import sys
import time
from bs4 import BeautifulSoup

from roster_stats import fetch_with_retry

TEAM_SLUGS = {
    "ANA": "ducks", "BOS": "bruins", "BUF": "sabres", "CAR": "hurricanes",
    "CBJ": "bluejackets", "CGY": "flames", "CHI": "blackhawks", "COL": "avalanche",
    "DAL": "stars", "DET": "redwings", "EDM": "oilers", "FLA": "panthers",
    "LAK": "kings", "MIN": "wild", "MTL": "canadiens", "NJD": "devils",
    "NSH": "predators", "NYI": "islanders", "NYR": "rangers", "OTT": "senators",
    "PHI": "flyers", "PIT": "penguins", "SEA": "kraken", "SJS": "sharks",
    "STL": "blues", "TBL": "lightning", "TOR": "mapleleafs", "UTA": "utah",
    "VAN": "canucks", "VGK": "goldenknights", "WPG": "jets", "WSH": "capitals"
}

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_team_injuries(abbrev, slug):
    """(players, error) - error is None on success (players may still be
    []: a genuinely clean injury report). fetch_with_retry already
    retries a connection error/timeout/429/5xx with backoff before
    giving up, so a None Response here means a real, retried-and-still-
    failing problem - error is set so the caller can tell that apart
    from "this team has no injuries," the exact distinction that was
    missing before (see roster_stats.py's docstring for the incident
    that made it matter for a different script)."""
    url = f"https://www.nhl.com/{slug}/team/injury-report"
    r = fetch_with_retry(url, headers=HEADERS, timeout=15, label=f"injury-report/{abbrev}")
    if r is None:
        return None, "fetch failed after retries"

    soup = BeautifulSoup(r.text, "html.parser")
    players = []

    # Find the heading that specifically says "Injured Reserve" and only
    # look at the table that immediately follows THAT heading — not just
    # any table on the page (the page also has roster/lineup tables that
    # look superficially similar).
    heading = None
    for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
        if "injured reserve" in tag.get_text(strip=True).lower():
            heading = tag
            break

    if heading is None:
        return [], None  # no injury section found at all on this page

    table = heading.find_next("table")
    if table is None:
        return [], None  # heading exists but no table follows (unusual, treat as empty)

    rows = table.find_all("tr")
    for row in rows[1:]:  # skip header row
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        first_cell_text = cells[0].get_text(strip=True)
        if not first_cell_text or "no current injured" in first_cell_text.lower():
            continue
        injury = cells[1].get_text(strip=True) if len(cells) > 1 else ""
        players.append({"player": first_cell_text, "injury": injury, "team": abbrev})

    return players, None


SCRATCHES_PATH = "data/scratches.json"


def load_previous_output():
    """See build_player_hub.py's load_previous_output() - same purpose:
    a per-team fallback source when this run's fetch fails for that team.
    None on a first-ever run or an unreadable file."""
    if not os.path.exists(SCRATCHES_PATH):
        return None
    try:
        with open(SCRATCHES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  could not read previous {SCRATCHES_PATH} for comparison: {e}")
        return None


def main():
    previous_output = load_previous_output()
    prev_details_by_team = {}
    for p in (previous_output or {}).get("details", []):
        prev_details_by_team.setdefault(p.get("team"), []).append(p)

    all_injured = []
    errors = []
    failed_teams = []

    print(f"Checking injury reports for {len(TEAM_SLUGS)} teams...")
    for i, (abbrev, slug) in enumerate(TEAM_SLUGS.items()):
        players, error = fetch_team_injuries(abbrev, slug)
        if error:
            errors.append((abbrev, error))
            failed_teams.append(abbrev)
            if abbrev in prev_details_by_team:
                # A team we couldn't check this run is not the same as a
                # team confirmed healthy - carrying over its last-known
                # injuries beats silently clearing them, the same "blanks,
                # not drops" rule build_player_hub.py follows for a
                # missing roster. Injury status is far more time-sensitive
                # than a roster or schedule though (see the coverage
                # guard below), so this is a one-run stopgap, not treated
                # as an acceptable steady state.
                carried = prev_details_by_team[abbrev]
                all_injured.extend(carried)
                print(f"  {abbrev}: ERROR - {error} - carried over {len(carried)} "
                      f"injured player(s) from the previous file")
            else:
                print(f"  {abbrev}: ERROR - {error} - no previous data to fall back on")
        else:
            if players:
                print(f"  {abbrev}: {len(players)} injured player(s) listed")
                all_injured.extend(players)
            else:
                print(f"  {abbrev}: none listed")
        # Same reasoning as the NHL API per-team delays elsewhere in this
        # repo's scripts - this scrapes a different host (nhl.com, not
        # api-web.nhle.com) so the specific throttling that confirmed-hit
        # build_player_hub.py isn't confirmed to apply here too, but
        # there's no reason to assume a fast, unbroken 32-request run
        # against it is safe just because it hasn't been caught yet.
        time.sleep(0.6)

    if len(failed_teams) / len(TEAM_SLUGS) > 0.2:
        # Unlike a roster or a season schedule, injury status is
        # genuinely time-sensitive - carrying over yesterday's list for
        # one or two unreachable teams is a reasonable stopgap, but
        # falling back for more than a fifth of the league at once means
        # the scrape itself is broken or blocked right now, not that
        # this many teams' pages individually happened to fail. Abort
        # and keep the previous file rather than publish a scratch list
        # that's mostly a day stale.
        print(f"\nABORTING: fetch failed after retries for {len(failed_teams)} of "
              f"{len(TEAM_SLUGS)} teams ({', '.join(failed_teams)}) - over 20%. "
              "Keeping the previous file on disk instead of committing one built mostly from stale fallbacks.")
        sys.exit(1)

    scratched_names = [p["player"] for p in all_injured]

    output = {
        "scratched": scratched_names,
        "details": all_injured,
        "source": "nhl.com official injury reports",
        "stale_teams": failed_teams,
        "note": "Auto-generated. If this list looks wrong or empty during the season, the page structure may have changed — flag for review."
    }

    with open(SCRATCHES_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{'='*50}")
    print(f"Total injured players found: {len(scratched_names)}")
    print(f"Teams with errors: {len(errors)}")
    if errors:
        for abbrev, err in errors:
            print(f"  - {abbrev}: {err}")
    if not scratched_names:
        print("\nNOTE: zero injuries found across the entire league. During the season")
        print("this would be very unusual — if this happens once games are underway,")
        print("it likely means the scraper broke (page redesign) rather than the league")
        print("being injury-free. Worth a sanity check against nhl.com directly.")


if __name__ == "__main__":
    main()
