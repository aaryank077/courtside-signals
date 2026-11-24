"""Reddit/ESPN discourse scraper.

Reads fan discourse — Reddit r/nba (when credentials are set) or ESPN NBA
news (always available, no auth) — and turns entity clusters into stat
questions the query engine can answer.

Reddit setup (optional, richer signal):
    Set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET environment variables,
    then `pip install praw`.  Create a free "script" app at
    https://www.reddit.com/prefs/apps to get the credentials.

ESPN fallback:
    Works without any setup.  Uses site.api.espn.com (public, no key).

Public API
----------
fetch_signals(sort="hot", limit=25) -> list[dict]

Each item:
    question      str   a question parse() understands
    category      str   the parse() category it maps to
    source_title  str   headline / thread title this came from
    source_url    str   link back to the source
    score         int   upvotes (Reddit) or 0 (ESPN)
"""

import json
import os
import re
import time
import urllib.request

from engine.parser import _find_players, normalize, parse

_ESPN_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news?limit={limit}"
)
_REDDIT_BASE = "https://www.reddit.com"
_USER_AGENT = "courtside-signals/0.1 discourse-engine"

_STAT_LABEL = {
    "pts": "points", "reb": "rebounds", "ast": "assists",
    "stl": "steals", "blk": "blocks", "tov": "turnovers",
    "fg3m": "three-pointers", "fg3_pct": "three-point percentage",
    "fg_pct": "field goal percentage", "ftm": "free throws",
    "min": "minutes",
}


# ── Source fetchers ─────────────────────────────────────────────────────────

def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _reddit_posts(sort, limit):
    """Fetch posts via PRAW.  Raises ImportError/KeyError if not configured."""
    import praw  # optional dep
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=_USER_AGENT,
    )
    posts = []
    for p in reddit.subreddit("nba").search("", sort=sort, time_filter="day", limit=limit):
        posts.append({
            "title": p.title,
            "body": getattr(p, "selftext", "") or "",
            "url": _REDDIT_BASE + p.permalink,
            "score": p.score,
        })
    return posts


def _espn_posts(limit):
    data = _get_json(_ESPN_URL.format(limit=limit))
    posts = []
    for a in data.get("articles", []):
        headline = a.get("headline") or a.get("title") or ""
        desc = a.get("description") or ""
        url = a.get("links", {}).get("web", {}).get("href") or ""
        posts.append({"title": headline, "body": desc, "url": url, "score": 0})
    return posts


def _fetch_posts(sort, limit):
    """Try Reddit first; fall back to ESPN if credentials are missing."""
    try:
        return _reddit_posts(sort, limit), "reddit"
    except (ImportError, KeyError):
        return _espn_posts(limit), "espn"
    except Exception:
        return _espn_posts(limit), "espn"


# ── Entity extraction ───────────────────────────────────────────────────────

def _entities(text):
    norm = normalize(text)
    players, found, _ = _find_players(norm)
    teams = [f["value"] for f in found if f["kind"] == "team"]
    stats = list(dict.fromkeys(f["value"] for f in found if f["kind"] == "stat"))
    return players, teams, stats


# ── Discourse themes ────────────────────────────────────────────────────────
#
# Each theme is a list of regex patterns plus the questions to generate.
# "require_all": True means every pattern must match (AND); False means any (OR).
# Themes handle abstract discourse that lacks specific player/stat mentions,
# e.g. "medical advances → longevity" → career-length All-NBA question.

_THEMES = [
    {
        # "are superstars older now?", "stars are aging", etc.
        "patterns": [
            r"\b(age|aging|ageing|older|younger|how old)\b",
            r"\b(superstars?|stars|all.?nba|elite players?|top players?)\b",
        ],
        "require_all": True,
        "questions": [
            ("Are superstars older now than earlier?", "all_nba"),
            ("Oldest All-NBA players", "all_nba"),
        ],
    },
    {
        # "longevity", "medical advances", "playing into their 40s", etc.
        "patterns": [
            r"\b(longevity|long career|career length|playing longer|last longer"
            r"|modern medicine|medical (advances?|technology|science)"
            r"|injury prevention|load management|keeping players healthy)\b",
        ],
        "require_all": False,
        "questions": [
            ("Average career length of All-NBA players over time", "all_nba"),
        ],
    },
    {
        # GOAT debates
        "patterns": [
            r"\b(goat|greatest of all time|best ever|greatest ever|greatest player"
            r"|g\.o\.a\.t)\b",
        ],
        "require_all": False,
        "questions": [
            ("Who has the most career points?", "all_time"),
            ("Who has the most career assists?", "all_time"),
        ],
    },
    {
        # Three-point revolution / shooting evolution
        "patterns": [
            r"\b(three.?point (revolution|era|shooting|explosion)"
            r"|beyond the arc|shooting from deep|pace and space"
            r"|volume shooter|long.?range shooting)\b",
        ],
        "require_all": False,
        "questions": [
            ("Who leads the league in three-pointers?", "league_leaders"),
            ("Will anyone beat Curry's 3-point record?", "all_time"),
        ],
    },
    {
        # Scoring era / offense up
        "patterns": [
            r"\b(scoring (era|explosion|is up|has risen|increased)"
            r"|high.?scoring|more points|offense is better|offensive era)\b",
        ],
        "require_all": False,
        "questions": [
            ("Who leads the league in points?", "league_leaders"),
        ],
    },
    {
        # Load management / rest / durability — raises questions about career
        # length, not whether superstars are older.
        "patterns": [
            r"\b(load management|rest games?|durability"
            r"|playing through (pain|injury)|availability is a skill)\b",
        ],
        "require_all": False,
        "questions": [
            ("Average career length of All-NBA players over time", "all_nba"),
        ],
    },
]


def _theme_questions(text):
    """Return (question, category) pairs triggered by discourse themes."""
    out = []
    for theme in _THEMES:
        patterns = theme["patterns"]
        if theme["require_all"]:
            matched = all(_has(p, text) for p in patterns)
        else:
            matched = any(_has(p, text) for p in patterns)
        if matched:
            out.extend(theme["questions"])
    return out


# ── Entity-based question templates ─────────────────────────────────────────

def _has(pattern, text):
    return bool(re.search(pattern, text, re.I))


def _stat_word(stats, fallback="points"):
    return _STAT_LABEL.get(stats[0], fallback) if stats else fallback


def _entity_questions(players, teams, stats, text):
    """Return (question, category) pairs derived from named entities."""
    out = []

    # Two-player comparison
    if len(players) >= 2:
        stat = _stat_word(stats)
        out.append((f"Compare {players[0]} vs {players[1]} {stat}", "compare_players"))

    # Single player
    elif len(players) == 1:
        p = players[0]
        if _has(r"\b(streak|consecutive|straight|in a row)\b", text):
            out.append((f"{p} longest streak of 30-point games", "streak"))
        elif _has(r"\b(career|all.?time|historic|greatest ever)\b", text):
            stat = _stat_word(stats)
            out.append((f"{p} career {stat}", "career_stat"))
        elif _has(r"\b(record|win loss|winning record|how often.*win)\b", text):
            out.append((f"{p}'s record when scoring 30+", "record"))
        elif _has(r"\b(lately|recent|last few|slump|hot streak|cold)\b", text):
            out.append((f"How has {p} played lately?", "recent_games"))
        elif _has(r"\b(best|greatest|peak|prime).*\b(season|year)\b", text) or \
             _has(r"\b(season|year).*\b(best|greatest|peak|prime)\b", text):
            stat = _stat_word(stats)
            out.append((f"{p}'s best {stat} season", "best_season"))
        else:
            stat = _stat_word(stats, "points, rebounds, and assists")
            out.append((f"What are {p}'s {stat} averages this season?", "player_averages"))

    # League leaders — no player, has a stat
    if not players and stats:
        if _has(r"\b(who|lead|leading|most|best|top|highest)\b", text):
            stat = _stat_word(stats)
            out.append((f"Who leads the league in {stat}?", "league_leaders"))

    return out


def _make_questions(players, teams, stats, text):
    """Combine theme detection + entity templates."""
    return _theme_questions(text) + _entity_questions(players, teams, stats, text)


# ── Main ────────────────────────────────────────────────────────────────────

def fetch_signals(sort="hot", limit=25):
    """Scrape r/nba (or ESPN) and return generated stat questions.

    Each returned item:
        question      str   question the query engine can answer
        category      str   parse() category
        source_title  str   originating headline / thread title
        source_url    str   link back to the source
        score         int   upvotes (Reddit) or 0 (ESPN fallback)
    """
    posts, source = _fetch_posts(sort, limit)
    seen = set()
    signals = []

    for post in posts:
        combined = " ".join(t for t in (post["title"], post["body"]) if t)
        players, teams, stats = _entities(combined)

        if not players and not teams and not stats:
            continue

        for question, _ in _make_questions(players, teams, stats, combined):
            if question in seen:
                continue
            cat, _ = parse(question)
            if cat == "unknown":
                continue
            seen.add(question)
            signals.append({
                "question": question,
                "category": cat,
                "source_title": post["title"],
                "source_url": post["url"],
                "score": post["score"],
                "source": source,
            })

    return sorted(signals, key=lambda s: -s["score"])
