#!/usr/bin/env python3
"""
Backfills data/historical_h2h_odds.json with real historical moneyline
(h2h) odds across four full NHL regular seasons (2022-23, 2023-24,
2024-25, 2025-26), snapshotted near puck drop as a closing-line proxy -
not the narrow 23-date slice in data/historical_prop_odds.json, which only
covers games the props pipeline (update_fantasy.py) had already logged.

IMPORTANT, learned from two live smoke tests:

1. The Odds API's historical /events endpoint does NOT return just the
   games for the snapshot date you pass it - a single snapshot returns
   every event currently listed as upcoming, and how far that extends
   is NOT fixed. At the season opener it spanned ~5 days forward; one
   real run through mid-season showed it repeatedly narrowing to 0-1
   days (bookmakers apparently often post lines only a day or so
   ahead once the season is underway). A fixed step under the
   opening-night window (2 days) produced dozens of confirmed
   near-misses once the season settled in - a real gap risk, not a
   theoretical one. So EVENT_DISCOVERY_STEP_DAYS defaults to 1 (sample
   every single day): every observed snapshot, even at its narrowest,
   still included the very next upcoming game night, so daily sampling
   never has to rely on a lookahead window at all. Discovery calls cost
   ~1 credit each (confirmed from live x-requests-remaining deltas, not
   the ~10/market/region rate that applies to the odds endpoint), so
   sampling daily instead of stepping is effectively free anyway.
2. Keying results by the snapshot date, rather than the game's own
   date, silently mis-dates any event a snapshot finds ahead of itself.
   So event discovery:
   a. Samples one snapshot per day (or every EVENT_DISCOVERY_STEP_DAYS,
      if you have a specific reason to widen it - see the warning this
      prints if the observed window ever gets narrower than that step).
   b. Merges everything into one dict keyed by the event's own id, so
      an event seen in several overlapping snapshots is only kept once.
   c. Derives each game's real date from its own commence_time (US/
      Eastern, matching the NHL's own local-date convention and how the
      rest of this repo dates games), NOT from whichever snapshot found it.

Then for each distinct event not already in the output file, fetches
the h2h market ~20 min before its own commence time: 1 market x 1
region (us) = 10 credits/call, per the pricing the user confirmed.

Resumable across all four seasons in one output file: re-running loads
the existing file and skips any (date, game) key already present, and
progress is saved to disk every SAVE_EVERY fetches (not just at the
end), so an interrupted run - or one that hits a rate limit - doesn't
lose games already paid for in credits. To add another season later,
just append to SEASONS below; already-fetched dates from prior seasons
are untouched and unbilled.

Season boundaries pulled from the NHL's own stats API
(api.nhle.com/stats/rest/en/season -> startDate / regularSeasonEndDate,
falling back to startDate when regularSeasonStartDate is blank, as it is
for 2022-23) on 2026-09-16 (2022-23 pulled 2026-09-18), except 2025-26
which keeps backtest_signal2_history.py's existing Oct 1 start (a few
days earlier than the NHL's own Oct 7 record) so this stays in sync with
that script.

Run:  python .github/scripts/backfill_h2h_odds.py [SEASON]
An optional SEASON argument (e.g. "2025-26") restricts discovery and
fetching to that one season - both the events cache and results file
stay shared across seasons, so this is just a scoping filter, not a
separate output. Omit it to run every season in SEASONS.
(Full four-season backfill is roughly 5,200 games -> ~52,000 credits
for the odds calls, plus ~750 discovery snapshots (one per day across
four seasons) at ~1 credit each - well inside a 100k/month allowance,
but it's a long-running job, safe to Ctrl-C and resume at any time. The
2022-23 season alone adds ~1,300 games / ~13,000 credits on top of
whatever the three-season run already fetched.)

Every save (results file and the discovery cache) writes via a temp file
plus atomic rename and keeps a rolling *.bak of the previous version, so
a process killed mid-write can only ever lose the newest in-progress
save, never truncate what was already safely on disk. Loading either
file validates it parses as JSON and refuses to continue (rather than
silently starting from empty) if it does not.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pytz
import requests

EASTERN = pytz.timezone("America/New_York")

API_KEY = os.environ.get("ODDS_API_KEY", "")
if not API_KEY:
    print("ODDS_API_KEY is not set in this terminal session.")
    raise SystemExit(1)

SEASONS = [
    ("2022-23", datetime(2022, 10, 7).date(), datetime(2023, 4, 14).date()),
    ("2023-24", datetime(2023, 10, 10).date(), datetime(2024, 4, 18).date()),
    ("2024-25", datetime(2024, 10, 4).date(), datetime(2025, 4, 17).date()),
    ("2025-26", datetime(2025, 10, 1).date(), datetime(2026, 4, 18).date()),
]

OUTPUT_PATH = "data/historical_h2h_odds.json"
EVENTS_CACHE_PATH = "data/historical_h2h_events_cache.json"  # raw discovery snapshots, so reruns don't re-bill them either
SAVE_EVERY = 10                    # fetched games between progress saves
REQUEST_SLEEP = 0.3                # seconds between odds calls
MAX_RETRIES = 4
EVENT_DISCOVERY_STEP_DAYS = 1        # daily - see module docstring on why a wider step isn't safe here


def load_json_or_fail(path):
    """Load a JSON cache/output file, refusing to silently continue on a
    corrupt file. A truncated file (e.g. from a process killed mid-write)
    must never be treated as "nothing fetched yet" - that would re-spend
    credits AND lose whatever the corrupt file still holds, since the next
    save() would overwrite it with a rebuild-from-empty result set.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"\n{path} exists but will not parse as JSON ({e}).\n"
            f"Refusing to proceed - starting from an empty dict here would "
            f"silently discard whatever this file still holds and re-spend "
            f"credits re-fetching it.\n"
            f"Check {path}.bak (written before every save) for the last "
            f"known-good version, or a Windows Previous Versions/File "
            f"History backup, before re-running."
        )


def atomic_write_json(path, data):
    """Write JSON via temp-file + rename, so a process kill mid-write can
    never truncate the file on disk - the old file (if any) stays intact
    until the new one is fully written and swapped in. Also keeps one
    rolling backup (path + '.bak') of the last known-good version.
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(tmp_path, path)


def local_game_date(commence_time_str):
    """The NHL's local (US/Eastern) calendar date for a game, from its UTC
    commence_time - e.g. a 7pm PT game has a UTC commence_time on the next
    calendar day, but is still "that night's" game in NHL scheduling terms.
    """
    utc_dt = datetime.strptime(commence_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
    return utc_dt.astimezone(EASTERN).date().strftime("%Y-%m-%d")


def _get(url, params):
    """GET with retry/backoff on rate limits and transient server errors.
    Returns the last response object even after exhausting retries, so the
    caller can inspect status_code/headers rather than getting None.
    """
    r = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, timeout=15)
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"    network error ({e}) - retrying in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 429:
            wait = 2 ** (attempt + 2)
            print(f"    rate limited - waiting {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            wait = 2 ** attempt
            print(f"    server error {r.status_code} - retrying in {wait}s")
            time.sleep(wait)
            continue
        return r
    return r


def get_day_events(date_str):
    """The day's NHL events per Odds API, snapshotted at 15:00 UTC - well
    before any NHL game that day has started (matches build_historical_odds.py).
    Returns (events_list, credits_remaining_str).
    """
    snapshot = f"{date_str}T15:00:00Z"
    url = "https://api.the-odds-api.com/v4/historical/sports/icehockey_nhl/events"
    r = _get(url, {"apiKey": API_KEY, "date": snapshot})
    if r is None or r.status_code != 200:
        code = r.status_code if r is not None else "no response"
        print(f"    events fetch failed for {date_str}: {code}")
        return [], (r.headers.get("x-requests-remaining", "?") if r is not None else "?")
    return r.json().get("data", []), r.headers.get("x-requests-remaining", "?")


def get_event_odds(event_id, near_time):
    """h2h odds for one event, snapshotted at near_time. Returns
    (odds_json_or_None, credits_remaining_str).
    """
    url = f"https://api.the-odds-api.com/v4/historical/sports/icehockey_nhl/events/{event_id}/odds"
    r = _get(url, {
        "apiKey": API_KEY,
        "date": near_time,
        "regions": "us",
        "markets": "h2h",
        "oddsFormat": "american",
    })
    if r is None or r.status_code != 200:
        code = r.status_code if r is not None else "no response"
        print(f"    odds fetch failed for event {event_id}: {code}")
        return None, (r.headers.get("x-requests-remaining", "?") if r is not None else "?")
    return r.json(), r.headers.get("x-requests-remaining", "?")


def season_for(date_str, seasons=SEASONS):
    for label, start, end in seasons:
        if start.strftime("%Y-%m-%d") <= date_str <= end.strftime("%Y-%m-%d"):
            return label
    return None


def build_snapshot_dates(seasons=SEASONS):
    """Sparse snapshot dates across every season passed in, stepped by
    EVENT_DISCOVERY_STEP_DAYS and capped at yesterday so we never ask the
    API for a day that hasn't happened (and doesn't have a closing line)
    yet - relevant if a season is still in progress.
    """
    today = datetime.now().date()
    dates = []
    for label, start, end in seasons:
        capped_end = min(end, today - timedelta(days=1))
        if capped_end < start:
            print(f"Skipping {label}: entirely in the future.")
            continue
        d = start
        season_dates = []
        while d <= capped_end:
            season_dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=EVENT_DISCOVERY_STEP_DAYS)
        # Always sample the season's actual last day too, in case the step
        # overshoots it.
        if season_dates and season_dates[-1] != capped_end.strftime("%Y-%m-%d"):
            season_dates.append(capped_end.strftime("%Y-%m-%d"))
        print(f"{label}: {start} -> {capped_end} ({len(season_dates)} snapshot calls, "
              f"stepped every {EVENT_DISCOVERY_STEP_DAYS}d)")
        dates.extend(season_dates)
    return dates


def discover_events(snapshot_dates):
    """Every distinct event seen across all snapshots, deduped by the
    event's own id (the same real game shows up in multiple overlapping
    snapshots - that's the point of stepping under the observed window).

    Discovery snapshots cost credits too, so results cache to
    EVENTS_CACHE_PATH keyed by snapshot date: a rerun skips any snapshot
    date it already sampled, and only queries genuinely new ones (e.g.
    from extending a season's end date, or a run resuming after being
    interrupted mid-discovery).

    Also prints the min/max commence_time span each snapshot actually
    returned, so a shrinking lookahead window would show up in the logs
    rather than silently creating a gap.
    """
    cache = {}
    if os.path.exists(EVENTS_CACHE_PATH):
        cache = load_json_or_fail(EVENTS_CACHE_PATH)
        print(f"Resuming discovery: {len(cache)} snapshot dates already sampled.")

    def save_cache():
        os.makedirs("data", exist_ok=True)
        atomic_write_json(EVENTS_CACHE_PATH, cache)

    fetched_since_save = 0
    try:
        for i, date_str in enumerate(snapshot_dates):
            if date_str in cache:
                events = cache[date_str]
            else:
                events, remaining = get_day_events(date_str)
                cache[date_str] = events
                fetched_since_save += 1
                time.sleep(REQUEST_SLEEP)
                if events:
                    times = sorted(e["commence_time"] for e in events)
                    span_days = (datetime.strptime(times[-1], "%Y-%m-%dT%H:%M:%SZ")
                                 - datetime.strptime(times[0], "%Y-%m-%dT%H:%M:%SZ")).days
                    print(f"  {date_str}: {len(events)} events, spanning {span_days}d "
                          f"({times[0]} -> {times[-1]}) (credits remaining: {remaining})")
                    # At the default of 1 (sample every day), a narrow span is
                    # normal - a single night's games don't need a lookahead
                    # window. This warning only means something if the step
                    # has been widened past 1.
                    if EVENT_DISCOVERY_STEP_DAYS > 1 and span_days < EVENT_DISCOVERY_STEP_DAYS:
                        print(f"    WARNING: lookahead window ({span_days}d) is narrower than the "
                              f"discovery step ({EVENT_DISCOVERY_STEP_DAYS}d) - a gap is possible near {date_str}.")
                else:
                    print(f"  {date_str}: 0 events (credits remaining: {remaining})")
                if fetched_since_save >= SAVE_EVERY:
                    save_cache()
                    fetched_since_save = 0
            if (i + 1) % 20 == 0:
                print(f"  ...discovery progress: {i + 1}/{len(snapshot_dates)} snapshots")
    finally:
        save_cache()

    events_by_id = {}
    for events in cache.values():
        for e in events:
            events_by_id[e["id"]] = e
    return events_by_id


def main():
    season_filter = sys.argv[1] if len(sys.argv) > 1 else None
    seasons = SEASONS
    if season_filter:
        seasons = [s for s in SEASONS if s[0] == season_filter]
        if not seasons:
            raise SystemExit(f"Unknown season {season_filter!r}. Choices: {[s[0] for s in SEASONS]}")
        print(f"Running backfill for {season_filter} only.\n")

    snapshot_dates = build_snapshot_dates(seasons)
    print(f"\nDiscovering events across {len(snapshot_dates)} snapshots...\n")
    events_by_id = discover_events(snapshot_dates)
    print(f"\nDiscovered {len(events_by_id)} distinct games across all seasons.\n")

    if season_filter:
        events_by_id = {
            eid: e for eid, e in events_by_id.items()
            if season_for(local_game_date(e["commence_time"])) == season_filter
        }
        print(f"Filtered to {len(events_by_id)} events in {season_filter}.\n")

    results = {}
    if os.path.exists(OUTPUT_PATH):
        results = load_json_or_fail(OUTPUT_PATH)
        print(f"Resuming: {len(results)} games already fetched.")

    print(f"Fetching h2h odds for events not already on disk...\n")

    fetched_since_save = 0
    misses = []
    credits_remaining = "?"
    new_games = 0

    def save():
        os.makedirs("data", exist_ok=True)
        atomic_write_json(OUTPUT_PATH, results)

    try:
        for i, e in enumerate(events_by_id.values()):
            date_str = local_game_date(e["commence_time"])
            game_label = f"{e['away_team']} @ {e['home_team']}"
            key = f"{date_str}|{game_label}"
            if key in results:
                continue

            try:
                commence = datetime.strptime(e["commence_time"], "%Y-%m-%dT%H:%M:%SZ")
                near_time = (commence - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                near_time = e["commence_time"]

            odds_data, credits_remaining = get_event_odds(e["id"], near_time)
            time.sleep(REQUEST_SLEEP)
            if odds_data is None:
                misses.append((date_str, game_label, "odds fetch failed"))
                continue

            results[key] = odds_data
            new_games += 1
            fetched_since_save += 1

            if fetched_since_save >= SAVE_EVERY:
                save()
                fetched_since_save = 0
                print(f"  ...progress saved: {len(results)} games total "
                      f"(credits remaining: {credits_remaining})")

            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(events_by_id)} events processed, "
                      f"{len(results)} games saved (credits remaining: {credits_remaining})")
    finally:
        # Always persist whatever was fetched, even on Ctrl-C or an
        # uncaught error partway through the season.
        save()

    print(f"\n{'='*50}")
    print(f"New games fetched this run: {new_games}")
    print(f"Total games in {OUTPUT_PATH}: {len(results)}")
    print(f"Misses: {len(misses)}")
    print(f"Credits remaining: {credits_remaining}")
    if misses:
        print("\nMissed games:")
        for date_str, game_label, reason in misses:
            print(f"  - {date_str} {game_label}: {reason}")


if __name__ == "__main__":
    main()
