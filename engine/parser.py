"""Rule-based question parser: plain-English question -> (category, params).

No LLM and no network calls. It finds the pieces of a question with word
lists and patterns (player, team, stats, numbers, season), then picks one of
the five categories with a few ordered keyword rules. The params dict it
builds is what engine.handlers.follow_query expects.

    >>> parse("How many 30-point games does Luka have against the Lakers?")
    ('game_count', {'player': 'Luka Dončić', 'opponent': 'LAL', 'conditions': {'pts': 30}})

When a question can't be parsed, the category is "unknown"; ask() logs those to
handlers.failed_queries so you can see which words or rules to add.
"""

import re
import unicodedata
from collections import Counter
from functools import lru_cache

from nba_api.stats.static import players

from engine.handlers import follow_query
from engine.vocab import (ATTEMPTS_FOR, ATTEMPTS_WORDS, CURRENT_SEASON, NICKNAMES,
                          NOT_NAMES, STATS, STOPWORDS, TEAMS)

PCT_STATS = {"fg_pct", "fg3_pct", "ft_pct"}
SEASON_RE = re.compile(r"\b((?:19|20)\d\d)\s*[-/]\s*(\d\d)\b")
NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(\s*\+|\s+or\s+more|\s+or\s+better|\s+plus)?")


def normalize(text):
    """Lowercase, drop accents and possessives, turn hyphens into spaces."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = text.replace("’", "'").replace("+/-", " plus minus ")
    text = re.sub(r"'s\b", "", text)
    text = text.replace("'", "").replace("-", " ")
    text = re.sub(r"[?,!;:()\"]|\.(?!\d)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- lookups

@lru_cache(maxsize=1)
def _phrases():
    """(phrase, kind, value) for stats, teams and nicknames, longest first."""
    out = [(p, "stat", stat) for stat, words in STATS.items() for p in words]
    out += [(p, "team", abbr) for abbr, words in TEAMS.items() for p in words]
    out += [(normalize(p), "player", name) for p, name in NICKNAMES.items()]
    out += [(p, "attempts", None) for p in ATTEMPTS_WORDS]
    out += [("games", "games", None), ("game", "games", None)]
    return sorted(out, key=lambda x: -len(x[0]))


@lru_cache(maxsize=1)
def _name_tables():
    """Normalized full names (all players) and unique last names (active)."""
    full = {}
    for p in sorted(players.get_players(), key=lambda p: not p["is_active"]):
        full.setdefault(normalize(p["full_name"]), p["full_name"])
    active = players.get_active_players()
    counts = Counter(normalize(p["last_name"]) for p in active)
    last = {normalize(p["last_name"]): p["full_name"] for p in active
            if counts[normalize(p["last_name"])] == 1}
    for word in NOT_NAMES | STOPWORDS:
        last.pop(word, None)
    return full, last


# ---------------------------------------------------------------- extraction

def _find_phrases(text, covered):
    """Match vocab phrases, longest first, never overlapping each other or the
    already-covered positions. Returns a sorted list of dicts and the updated
    set of covered character positions."""
    covered, found = set(covered), []
    for phrase, kind, value in _phrases():
        pattern = r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])"
        for m in re.finditer(pattern, text):
            span = set(range(m.start(), m.end()))
            if span & covered:
                continue
            covered |= span
            found.append({"kind": kind, "value": value,
                          "start": m.start(), "end": m.end()})
    return sorted(found, key=lambda f: f["start"]), covered


def _find_name(text, covered, sizes, table):
    """First run of `sizes` adjacent words found in `table`.
    Returns (full_name, covered positions) or (None, set())."""
    tokens = [m for m in re.finditer(r"[a-z0-9]+", text)
              if not set(range(m.start(), m.end())) & covered]
    for n in sizes:
        for i in range(len(tokens) - n + 1):
            window = tokens[i:i + n]
            # words must be next to each other in the question
            if any(window[j + 1].start() - window[j].end() > 1 for j in range(n - 1)):
                continue
            key = " ".join(m.group() for m in window)
            if key in table:
                return table[key], set(range(window[0].start(), window[-1].end()))
    return None, set()


def _find_all_names(text, covered, sizes, table):
    """Every name from `table` in the text, as (position, full_name)."""
    names = []
    while True:
        name, span = _find_name(text, covered, sizes, table)
        if not name:
            return names, covered
        names.append((min(span), name))
        covered = covered | span


def _find_players(text):
    """Find every player named, then match the remaining words against the vocab.

    Order matters: full names first ("LeBron James" before the nickname
    "lebron"), then nicknames, then unique last names of active players.
    Returns players in the order they appear, without repeats.
    """
    full, last = _name_tables()
    names, covered = _find_all_names(text, set(), (4, 3, 2), full)
    found, covered = _find_phrases(text, covered)
    names += [(f["start"], f["value"]) for f in found if f["kind"] == "player"]
    more, covered = _find_all_names(text, covered, (2, 1), last)
    names += more
    found = [f for f in found if f["kind"] != "player"]
    players = list(dict.fromkeys(name for _, name in sorted(names)))
    return players, found, covered


def _pair_numbers(text, found, covered):
    """Attach each number to the stat (or 'games'/'attempts') right after it,
    e.g. '30 point games' -> ('pts', 30), 'at least 400 attempts' -> ('attempts', 400)."""
    pairs = []
    for m in NUMBER_RE.finditer(text):
        if m.start() in covered:
            continue
        value = float(m.group(1))
        value = int(value) if value.is_integer() else value
        nxt = next((f for f in found if f["start"] >= m.end()), None)
        if nxt and re.fullmatch(r"\s*", text[m.end():nxt["start"]]):
            nxt["paired"] = True
            pairs.append((nxt["kind"], nxt["value"], value))
        elif re.search(r"\b(dropped|drop|drops|scored|score|scores|scoring|put up|puts up|had)\s+$",
                       text[:m.start()]):
            # a bare number after a scoring verb: "dropped 40 on the Clippers"
            pairs.append(("stat", "pts", value))
    return pairs


def _extract(question):
    text = normalize(question)

    season = None
    m = SEASON_RE.search(question.lower())
    if m:
        season = f"{m.group(1)}-{m.group(2)}"
        text = normalize(SEASON_RE.sub(" ", question.lower()))
    elif re.search(r"\b(this|current) (season|year)\b", text):
        season = CURRENT_SEASON
    elif re.search(r"\blast (season|year)\b", text):
        start = int(CURRENT_SEASON[:4]) - 1
        season = f"{start}-{str(start + 1)[-2:]}"

    players_found, found, covered = _find_players(text)

    # an uppercase abbreviation like "LAL" or "BOS" in the original question
    teams = [f["value"] for f in found if f["kind"] == "team"]
    teams += [a for a in re.findall(r"\b[A-Z]{3}\b", question) if a in TEAMS]

    limit = None
    m = re.search(r"\btop (\d+)\b", text)
    if m:
        limit = int(m.group(1))
        text = text[:m.start()] + "top " + " " * len(m.group(1)) + text[m.end():]

    pairs = _pair_numbers(text, found, covered)
    stats = list(dict.fromkeys(f["value"] for f in found
                               if f["kind"] == "stat" and not f.get("paired")))

    home_away = None
    if re.search(r"\bat home\b|\bhome games\b", text):
        home_away = "home"
    elif re.search(r"\bon the road\b|\baway games\b|\broad games\b", text):
        home_away = "away"

    return {"text": text, "players": players_found,
            "player": players_found[0] if players_found else None,
            "team": teams[0] if teams else None,
            "season": season, "stats": stats, "pairs": pairs, "limit": limit,
            "home_away": home_away}


def _thresholds(pairs, pct_stat=None):
    """Split number pairs into stat minimums, min_games, and min_minutes."""
    conditions, extra = {}, {}
    for kind, stat, value in pairs:
        if kind == "games":
            extra["min_games"] = value
        elif kind == "attempts":
            conditions[ATTEMPTS_FOR.get(pct_stat, "fga")] = value
        elif kind == "stat":
            conditions[stat] = value
    return conditions, extra


# ---------------------------------------------------------------- intent

def parse(question):
    """Return (category, params) for a question, or ("unknown", {})."""
    x = _extract(question)
    text, player, stats = x["text"], x["player"], x["stats"]
    has = lambda pattern: re.search(pattern, text) is not None

    def add_filters(params, opponent=True):
        if opponent and x["team"]:
            params["opponent"] = x["team"]
        if x["season"]:
            params["season"] = x["season"]
        if x["home_away"]:
            params["home_away"] = x["home_away"]
        return params

    triple_double = has(r"\btriple doubles?\b")

    def game_conditions():
        conditions, _ = _thresholds(x["pairs"])
        if triple_double:
            # approximation: nearly every triple-double is points/rebounds/assists
            conditions.update({"pts": 10, "reb": 10, "ast": 10})
        return conditions

    # compare: two or more players named
    if len(x["players"]) >= 2:
        return "compare_players", add_filters({"players": x["players"],
                                               "stats": stats or ["pts", "reb", "ast"]})

    # win-loss record: "Luka's record when he scores 40+", "Curry's record vs the Lakers"
    if player and has(r"\b(record|win loss|win percentage|win pct|win %)\b|\bhow many (wins|games did \w+ win)\b"):
        params = {"player": player}
        conditions = game_conditions()
        if conditions:
            params["conditions"] = conditions
        return "record", add_filters(params)

    # streaks: "longest streak of 30 point games", "triple doubles in a row"
    if player and has(r"\b(streaks?|straight|consecutive|in a row)\b"):
        conditions = game_conditions()
        if conditions:
            return "streak", add_filters({"player": player, "conditions": conditions})

    # recent form: "Curry's last 10 games", "how has Luka played lately"
    m = re.search(r"\blast (\d+)\b", text)
    if player and (m or has(r"\b(lately|recently|recent|last few games|past few games)\b")):
        params = {"player": player, "n": int(m.group(1)) if m else 5}
        if stats:
            params["stats"] = stats
        return "recent_games", add_filters(params)

    # single-game highs: "career high", "most points in a game"
    single_game = has(r"\b(career highs?|in (a|one) game|single game|in a night|one night)\b")
    if player and single_game and not x["pairs"]:
        params = {"player": player, "stat": stats[0] if stats else "pts"}
        if has(r"\b(lowest|fewest|worst)\b"):
            params["ascending"] = True
        if x["limit"]:
            params["limit"] = x["limit"]
        return "best_games", add_filters(params)

    # career totals
    if player and has(r"\bcareer\b") and not has(r"\bseason\b"):
        return "career_stat", {"player": player, "stats": stats or ["pts", "reb", "ast"]}

    # counting games: "how many 30 point games", "how many triple doubles"
    if player and (x["pairs"] or triple_double) and has(r"\bhow many\b|\bnumber of\b|\bcount\b|\bhow often\b|\bever\b|\btimes\b"):
        conditions = game_conditions()
        if conditions:
            return "game_count", add_filters({"player": player, "conditions": conditions})

    # best / worst season
    if player and has(r"\bseasons?\b") and has(r"\b(best|worst|top|highest|lowest|greatest)\b"):
        stat = stats[0] if stats else "pts"
        conditions, extra = _thresholds(x["pairs"], stat)
        params = {"player": player, "stat": stat, **extra}
        if conditions:
            params["min"] = conditions
        if has(r"\b(worst|lowest)\b") and stat != "tov":
            params["ascending"] = True
        return "best_season", params

    # league leaders: no player named, a stat, and asks who leads / is top
    if not player and stats and has(r"\b(who|leads?|leaders?|leading|top|most|highest|lowest|fewest|best)\b"):
        stat = stats[0]
        conditions, extra = _thresholds(x["pairs"], stat)
        params = {"stat": stat, **extra}
        if "min" in conditions:
            params["min_minutes"] = conditions.pop("min")
        if conditions:
            params["min"] = conditions
        if x["team"]:
            params["team"] = x["team"]
        if x["limit"]:
            params["limit"] = x["limit"]
        if x["season"]:
            params["season"] = x["season"]
        if has(r"\btotals?\b"):
            params["per"] = "total"
        if has(r"\b(lowest|fewest)\b"):
            params["ascending"] = True
        return "league_leaders", params

    # a player's numbers, optionally vs a team / in a season / home or away
    if player and (stats or has(r"\b(averag\w*|numbers|stats|statline|line|doing)\b")):
        return "player_averages", add_filters({"player": player,
                                               "stats": stats or ["pts", "reb", "ast"]})

    return "unknown", {}


def ask(question):
    """Parse a question and answer it. Unparsed questions go to failed_queries."""
    category, params = parse(question)
    if category == "unknown":
        result = follow_query("unknown", question)
    else:
        result = follow_query(category, params)
    return {**result, "question": question, "params": params}
