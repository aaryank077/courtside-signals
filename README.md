# courtside-signals

Ask NBA stat questions in plain English and get an answer plus the table
behind it, StatMuse-style.

> **How many 30-point games does Luka have against the Lakers?**
> Luka Dončić has 8 games with 30+ pts vs LAL.

Everything runs locally and costs nothing: questions are parsed with word
lists and rules (no LLM), and data comes from the free
[nba_api](https://github.com/swar/nba_api) into a SQLite database.

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python seed.py            # optional: pre-loads Curry, Luka and Jokić
venv/bin/python seed.py --all-nba  # needed for All-NBA / superstar questions (~4 min, once)
```

## Run it

**Web app:** start the server, then open http://localhost:8000.

```bash
venv/bin/python server.py
```

**Terminal:**

```bash
venv/bin/python ask.py                                  # type questions; "exit" to quit
venv/bin/python ask.py "LeBron career high"             # one question
venv/bin/python ask.py --debug "Curry vs Luka points"   # also show how it was parsed
```

The first question about a player fetches their whole career (10–30 seconds).
After that it's instant, and their current season refreshes once a day.

## What it can answer

| Category | Example |
|---|---|
| `player_averages` | What are Jokić's rebound numbers against the Celtics? |
| `game_count` | How many triple-doubles does Jokic have this season? |
| `best_season` | Steph Curry's best 3-point shooting season with at least 400 attempts |
| `best_games` | Most points in a game Steph Curry |
| `career_stat` | Shai Gilgeous-Alexander career points |
| `league_leaders` | Who leads the league in assists among players averaging 30+ minutes? |
| `compare_players` | Curry vs Luka points per game |
| `record` | What's Luka's record when he scores 40+? |
| `streak` | Jokic's longest streak of triple-doubles |
| `recent_games` | How has Wembanyama played lately? |
| `all_nba` | Are superstars older now than earlier? · Oldest All-NBA players · How many All-NBA teams does LeBron have? · Average career length of All-NBA players over time |

"Superstars" means All-NBA selections (from `all-nba.csv`, via Basketball
Reference). Ages are on Feb 1 of each season. Age and career-length questions
come with a chart.

Filters work across most of these: an opponent ("vs the Lakers", "LAL"), a
season ("2023-24", "this season", "last season"), and "at home" / "on the road".

## How it works

```
question ─► engine/parser.py ─► (category, params) ─► engine/handlers.py ─► SQL on courtside.db ─► answer + table
                 │                                          │
           engine/vocab.py                           engine/fetch.py
   (teams, stat words, nicknames)        (fetches players/league stats from nba_api as needed)
```

1. **Parse** (`engine/parser.py`): finds the players, team, stats, numbers
   ("30-point", "10+ assists", "at least 400 attempts") and season in the
   question, then picks a category with a few ordered keyword rules.
2. **Answer** (`engine/handlers.py`): one function per category takes a params
   dict and runs SQL against `courtside.db`. Each returns
   `{answer, table, data}`.
3. **Fetch** (`engine/fetch.py`): a player not yet in the database gets their
   full career of game logs and career totals saved on first use. League
   stats refresh once a day.

Questions it can't parse are logged to `failed_queries` in `engine/handlers.py`.

## Project layout

| Path | What it is |
|---|---|
| `server.py`, `web/index.html` | local web app and its JSON API (`/api/ask?q=...`) |
| `ask.py` | terminal interface |
| `engine/parser.py` | question → category + params |
| `engine/vocab.py` | team names, stat synonyms, player nicknames, `CURRENT_SEASON` |
| `engine/handlers.py` | the eleven categories |
| `engine/fetch.py` | on-demand nba_api fetching and daily refresh |
| `engine/allnba.py`, `all-nba.csv` | All-NBA selections and player birthdates/career spans |
| `seed.py`, `schema.py` | optional pre-loading; database path and `games` table |
| `tests/test_parser.py` | parser and end-to-end answer tests |
| `ARCHITECTURE.md` | the longer-term plan (discourse engine, frontend) |

## Tests

```bash
venv/bin/python tests/test_parser.py
```

Tests never call nba_api (`COURTSIDE_OFFLINE=1`), so they're fast and run
offline. Answer checks need a seeded `courtside.db`.

## Teaching it new words

When a question fails, it's usually a missing word. Add it to
`engine/vocab.py` (a team nickname, stat synonym or player nickname), then add
the question and its expected parse to `PARSE_CASES` in the tests.

When a new season starts, change `CURRENT_SEASON` in `engine/vocab.py`.

## Limits

- Regular season only; no playoffs yet.
- League-wide game questions ("who has the most 40-point games?") only see
  players already in the database. Leaderboards use season totals for every
  player, so they're complete.
- Common words that are also last names ("Brown", "Green", "Young") need the
  player's full name.
- Triple-doubles are counted as 10+ points, rebounds and assists, which misses
  rare ones built on steals or blocks.
