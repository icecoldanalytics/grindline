#!/usr/bin/env python3
"""
Single source of truth for current-32-team NHL name/abbreviation data -
the same "one canonical module, everyone imports it" role rest_edge.py
plays for the signal definition itself.

Before this existed, update_dashboard.py and update_scores.py each kept
their own separate copy of the city-name dict (confirmed byte-identical
across all 32 teams at merge time, but nothing enforced that - editing
one without the other would silently break live score-card matching in
index.html's updateLiveScores(), which joins dashboard-rendered cards to
scores.json updates by comparing city-name text). Merging them here
removes that failure mode instead of just noting it.

FULL_NAMES / CITY_NAMES / TEAM_NAMES source: update_dashboard.py (the
more complete of the two pre-merge copies). NAME_MAP (lowercase city/
name fragments for fuzzy-matching against The Odds API's full team-name
strings) was previously only in update_dashboard.py; kept here since
it's the same underlying team-identity data, not something distinct.

Scripts elsewhere in this repo (capture_signals.py, build_schedule_analysis.py,
build_season_board.py) still keep their own separate copies as of this
module's creation - only update_dashboard.py and update_scores.py were
asked to be merged. Migrating the rest is a reasonable follow-up, not
done here.
"""

NAME_MAP = {  # abbrev (lowercase) -> lowercase city/name fragment, for Odds API team matching
    "tor": "toronto", "fla": "florida", "bos": "boston", "buf": "buffalo",
    "mtl": "montreal", "ott": "ottawa", "det": "detroit", "tbl": "tampa",
    "car": "carolina", "nyr": "new york rangers", "nyi": "new york islanders",
    "njd": "new jersey", "phi": "philadelphia", "pit": "pittsburgh",
    "wsh": "washington", "cbj": "columbus", "chi": "chicago",
    "nsh": "nashville", "stl": "st. louis", "min": "minnesota",
    "wpg": "winnipeg", "col": "colorado", "uta": "utah", "cgy": "calgary",
    "edm": "edmonton", "van": "vancouver", "sea": "seattle",
    "lak": "los angeles", "ana": "anaheim", "sjs": "san jose",
    "vgk": "vegas", "dal": "dallas",
}

FULL_NAMES = {
    "TOR": "Toronto Maple Leafs", "FLA": "Florida Panthers", "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres", "MTL": "Montréal Canadiens", "OTT": "Ottawa Senators",
    "DET": "Detroit Red Wings", "TBL": "Tampa Bay Lightning", "CAR": "Carolina Hurricanes",
    "NYR": "New York Rangers", "NYI": "New York Islanders", "NJD": "New Jersey Devils",
    "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins", "WSH": "Washington Capitals",
    "CBJ": "Columbus Blue Jackets", "CHI": "Chicago Blackhawks", "NSH": "Nashville Predators",
    "STL": "St. Louis Blues", "MIN": "Minnesota Wild", "WPG": "Winnipeg Jets",
    "COL": "Colorado Avalanche", "UTA": "Utah Mammoth", "CGY": "Calgary Flames",
    "EDM": "Edmonton Oilers", "VAN": "Vancouver Canucks", "SEA": "Seattle Kraken",
    "LAK": "Los Angeles Kings", "ANA": "Anaheim Ducks", "SJS": "San Jose Sharks",
    "VGK": "Vegas Golden Knights", "DAL": "Dallas Stars",
}

CITY_NAMES = {
    "TOR": "Toronto", "FLA": "Florida", "BOS": "Boston", "BUF": "Buffalo",
    "MTL": "Montréal", "OTT": "Ottawa", "DET": "Detroit", "TBL": "Tampa Bay",
    "CAR": "Carolina", "NYR": "New York", "NYI": "New York", "NJD": "New Jersey",
    "PHI": "Philadelphia", "PIT": "Pittsburgh", "WSH": "Washington", "CBJ": "Columbus",
    "CHI": "Chicago", "NSH": "Nashville", "STL": "St. Louis", "MIN": "Minnesota",
    "WPG": "Winnipeg", "COL": "Colorado", "UTA": "Utah", "CGY": "Calgary",
    "EDM": "Edmonton", "VAN": "Vancouver", "SEA": "Seattle", "LAK": "Los Angeles",
    "ANA": "Anaheim", "SJS": "San Jose", "VGK": "Vegas", "DAL": "Dallas",
}

TEAM_NAMES = {
    "TOR": "Maple Leafs", "FLA": "Panthers", "BOS": "Bruins", "BUF": "Sabres",
    "MTL": "Canadiens", "OTT": "Senators", "DET": "Red Wings", "TBL": "Lightning",
    "CAR": "Hurricanes", "NYR": "Rangers", "NYI": "Islanders", "NJD": "Devils",
    "PHI": "Flyers", "PIT": "Penguins", "WSH": "Capitals", "CBJ": "Blue Jackets",
    "CHI": "Blackhawks", "NSH": "Predators", "STL": "Blues", "MIN": "Wild",
    "WPG": "Jets", "COL": "Avalanche", "UTA": "Mammoth", "CGY": "Flames",
    "EDM": "Oilers", "VAN": "Canucks", "SEA": "Kraken", "LAK": "Kings",
    "ANA": "Ducks", "SJS": "Sharks", "VGK": "Golden Knights", "DAL": "Stars",
}
