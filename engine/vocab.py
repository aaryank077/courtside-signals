"""Word lists the question parser matches against.

Everything is lowercase, accent-free, with hyphens written as spaces, the same
way parser.normalize() rewrites questions. Add to these when a question in
failed_queries uses a word the parser didn't know.
"""

# The season the league_stats table holds and "this season" refers to.
# seed.py imports this, so change it here when a new season starts.
CURRENT_SEASON = "2026-27"

# Team abbreviation -> words fans use for it. "la" / "los angeles" are left out
# on purpose because they could mean the Lakers or the Clippers.
TEAMS = {
    "ATL": ["hawks", "atlanta"],
    "BOS": ["celtics", "boston", "celts"],
    "BKN": ["nets", "brooklyn"],
    "CHA": ["hornets", "charlotte"],
    "CHI": ["bulls", "chicago"],
    "CLE": ["cavaliers", "cavs", "cleveland"],
    "DAL": ["mavericks", "mavs", "dallas"],
    "DEN": ["nuggets", "denver"],
    "DET": ["pistons", "detroit"],
    "GSW": ["warriors", "golden state", "dubs"],
    "HOU": ["rockets", "houston"],
    "IND": ["pacers", "indiana"],
    "LAC": ["clippers", "clips"],
    "LAL": ["lakers"],
    "MEM": ["grizzlies", "grizz", "memphis"],
    "MIA": ["heat", "miami"],
    "MIL": ["bucks", "milwaukee"],
    "MIN": ["timberwolves", "wolves", "minnesota"],
    "NOP": ["pelicans", "pels", "new orleans"],
    "NYK": ["knicks", "new york"],
    "OKC": ["thunder", "oklahoma city", "okc"],
    "ORL": ["magic", "orlando"],
    "PHI": ["76ers", "sixers", "philadelphia", "philly"],
    "PHX": ["suns", "phoenix"],
    "POR": ["trail blazers", "blazers", "portland"],
    "SAC": ["kings", "sacramento"],
    "SAS": ["spurs", "san antonio"],
    "TOR": ["raptors", "toronto"],
    "UTA": ["jazz", "utah"],
    "WAS": ["wizards", "washington"],
}

# Phrase -> stat column. Longer phrases are matched first, so "3 point
# attempts" wins over "3 point" and "free throw percentage" over "free throws".
STATS = {
    "pts": ["points", "point", "pts", "pt", "ppg", "scoring", "scored", "score"],
    "reb": ["rebounds", "rebound", "rebounding", "boards", "reb", "rebs", "rpg"],
    "ast": ["assists", "assist", "dimes", "ast", "apg"],
    "stl": ["steals", "steal", "stl", "spg"],
    "blk": ["blocks", "block", "blocked shots", "blk", "bpg"],
    "tov": ["turnovers", "turnover", "tov", "giveaways"],
    "min": ["minutes", "minute", "mins", "mpg"],
    "oreb": ["offensive rebounds", "offensive boards"],
    "dreb": ["defensive rebounds", "defensive boards"],
    "fg3m": ["3 pointers", "3 pointer", "threes", "three pointers", "three pointer",
             "3s", "3pm", "made threes", "made 3s", "triples"],
    "fg3a": ["3 point attempts", "three point attempts", "3pa", "3 pt attempts",
             "three attempts", "3 attempts"],
    "fg3_pct": ["3 point percentage", "three point percentage", "3p%", "3pt%",
                "3 pt%", "3 point shooting", "three point shooting",
                "shooting from three", "shooting from deep", "from three", "from deep"],
    "fga": ["field goal attempts", "shot attempts", "fga"],
    "fgm": ["field goals made", "made field goals", "fgm", "field goals"],
    "fg_pct": ["field goal percentage", "fg%", "fg percentage", "shooting percentage",
               "shooting", "efficiency"],
    "ftm": ["free throws made", "made free throws", "free throws", "ftm"],
    "fta": ["free throw attempts", "fta"],
    "ft_pct": ["free throw percentage", "ft%", "free throw shooting"],
    "plus_minus": ["plus minus"],
}

# "attempts" on its own means the attempts behind whatever percentage was asked
# about ("best 3 point shooting season with at least 400 attempts").
ATTEMPTS_WORDS = ["attempts", "attempted", "shots"]
ATTEMPTS_FOR = {"fg3_pct": "fg3a", "fg_pct": "fga", "ft_pct": "fta"}

# Short names fans use that aren't a unique last name.
NICKNAMES = {
    "steph": "Stephen Curry",
    "luka": "Luka Dončić",
    "lebron": "LeBron James",
    "bron": "LeBron James",
    "giannis": "Giannis Antetokounmpo",
    "sga": "Shai Gilgeous-Alexander",
    "shai": "Shai Gilgeous-Alexander",
    "wemby": "Victor Wembanyama",
    "kd": "Kevin Durant",
    "ad": "Anthony Davis",
    "jokic": "Nikola Jokić",
    "joker": "Nikola Jokić",
    "embiid": "Joel Embiid",
    "jimmy": "Jimmy Butler III",
    "ant": "Anthony Edwards",
    "ja": "Ja Morant",
    "kawhi": "Kawhi Leonard",
    "cade": "Cade Cunningham",
    "tatum": "Jayson Tatum",
    "brunson": "Jalen Brunson",
    "dame": "Damian Lillard",
    "book": "Devin Booker",
}

# Words that are also active players' last names but usually aren't meant that
# way in a question. Use the full name for these players.
NOT_NAMES = {
    "young", "green", "brown", "white", "black", "holiday", "bridges", "hunter",
    "walker", "wells", "hart", "ball", "mann", "love", "post", "small", "battle",
    "grant", "fox", "cain", "allen", "james", "jordan", "jones", "smith", "martin",
    "thomas", "miller", "davis", "harris", "johnson", "williams", "price", "dick",
}

# Common question words, never treated as a player's last name.
STOPWORDS = {
    "the", "a", "an", "how", "many", "much", "who", "what", "which", "when", "in",
    "of", "on", "at", "to", "for", "by", "has", "have", "had", "does", "did", "do",
    "is", "was", "are", "with", "against", "vs", "versus", "this", "that", "his",
    "her", "their", "season", "seasons", "game", "games", "per", "league", "most",
    "best", "worst", "top", "career", "average", "averages", "averaging", "numbers",
    "stats", "least", "more", "or", "and", "than", "over", "under", "home", "road",
    "away", "total", "totals", "among", "players", "player", "leads", "leader",
    "leaders", "lead", "ever", "all", "time", "year", "highest", "lowest", "fewest",
    "long", "king", "will", "night", "nights", "get", "got", "put", "up", "dropped",
}
