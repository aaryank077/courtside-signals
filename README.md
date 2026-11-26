# courtside-signals

[![CI](https://github.com/aaryank077/courtside-signals/actions/workflows/ci.yml/badge.svg)](https://github.com/aaryank077/courtside-signals/actions/workflows/ci.yml)

Ask NBA stat questions in plain English and get an answer plus the table
behind it, StatMuse-style.

> **How many 30-point games does Luka have against the Lakers?**
> Luka Dončić has 8 games with 30+ pts vs LAL.

Questions are parsed with word lists and rules first, which runs locally and
costs nothing. When the rules can't answer, an optional LLM fallback has Claude
write one read-only SQL query instead. Both are scored on the same 116-question
test set (see [Rules vs LLM](#rules-vs-llm-text-to-sql)). Data comes from the free
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
venv/bin/python ask.py --llm "Jokic plus-minus in losses"   # rules, then Claude if they can't
```

`--llm` needs an Anthropic API key in your shell (`export ANTHROPIC_API_KEY=...`);
nothing else reads or stores it.

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
| `all_time` | Will anyone beat Curry's 3-point record? · Who has the most career points? · Where does LeBron rank all-time in assists? |
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

## Rules vs LLM text-to-SQL

`engine/llm_sql.py` is a second way to answer: Claude (`claude-opus-5-5`, low
effort) gets the schema, a page of notes on how the data is encoded, and six
worked examples, and returns `{"sql": ..., "cannot_answer": ...}` as structured
JSON. `ask_hybrid()` tries the rules first and calls the model only when they
can't parse the question or their handler fails.

The model's SQL is treated as untrusted:

- the database is opened read-only (`mode=ro`) with `PRAGMA query_only`
- a SQLite authorizer allows only SELECT and reads, so
  `WITH x AS (...) DELETE ...`, `PRAGMA` and `ATTACH` are refused before they run
- one statement per call, a 2-second timeout (progress handler), 200 rows max
- a failed query goes back to the model once with the error; a write is never retried

### How it's measured

[`evals/questions.jsonl`](evals/questions.jsonl) has 116 questions: 6–9 in each
of the 12 categories (some reworded so the rules don't recognize them), 20 that
the database can answer but no rule category covers, and 9 that it can't
(playoffs, salaries, players not loaded, "delete all of Curry's games").

- **Correct means the right values, not the right SQL.** Each question stores
  a hand-written gold query and the rows it returns on
  [`evals/snapshot.db`](evals/snapshot.db), a frozen copy of the database, so
  answers don't drift as games are played. An answer is right when those
  values appear in it: counts exactly, averages within rounding, names without
  accents, the pick first for "who leads..." questions, each player's numbers
  next to his name for comparisons. For an unanswerable question, the right
  answer is no answer.
- **Same test for both.** The rules are graded on their answer sentence and
  data; the LLM on the rows its SQL returned.
- CI re-runs every gold query against the snapshot and checks the stored answers.

```bash
venv/bin/python -m evals.run --rules-only               # free, no API calls
venv/bin/python -m evals.run                            # rules + Claude Opus 5.5 (paid API; reports cost per question)
venv/bin/python -m evals.run --model claude-sonnet-5-5
venv/bin/python -m evals.run --model ollama/qwen2.5-coder:3b   # free, runs locally
venv/bin/python -m evals.run --report                   # tables from evals/results/
```

**Free local model.** Any model named `ollama/<name>` runs on a local
[Ollama](https://ollama.com) server instead of the Anthropic API, with the same
prompt, the same JSON output schema and the same SQL guards. Start the server,
pull a model, then run the eval:

```bash
ollama serve &
ollama pull qwen2.5-coder:3b
venv/bin/python -m evals.run --model ollama/qwen2.5-coder:3b
```

### Results

Measured with a free local model, `qwen2.5-coder:3b` on Ollama, running on a
2020 Intel MacBook CPU. Claude hasn't been run yet; its row goes here when it
is. Per-question results, including every generated query, are in
[`evals/results/`](evals/results/).

**Accuracy by category** (`ollama/qwen2.5-coder:3b`, 116 questions)

| Category | n | Rules | LLM | Rules → LLM fallback |
|---|---:|---:|---:|---:|
| `player_averages` | 8 | 100% | 38% | 100% |
| `game_count` | 9 | 100% | 78% | 100% |
| `best_season` | 7 | 86% | 57% | 86% |
| `best_games` | 8 | 88% | 62% | 88% |
| `career_stat` | 7 | 100% | 100% | 100% |
| `league_leaders` | 7 | 100% | 43% | 100% |
| `compare_players` | 6 | 83% | 0% | 83% |
| `record` | 7 | 86% | 57% | 86% |
| `streak` | 6 | 100% | 100% | 100% |
| `recent_games` | 7 | 100% | 0% | 100% |
| `all_time` | 7 | 100% | 29% | 100% |
| `all_nba` | 8 | 100% | 50% | 100% |
| `beyond_rules` | 20 | 5% | 75% | 25% |
| `unanswerable` | 9 | 89% | 100% | 89% |
| **12 rule categories** | 87 | 95% | 52% | 95% |
| **All** | 116 | 79% | 59% | 83% |

**Latency and cost per question**

| | p50 latency | p95 latency | cost / question | LLM calls |
|---|---:|---:|---:|---:|
| Rules | 31.4 ms | 197.4 ms | $0.0000 | 0 |
| LLM | 5.0 s | 16.8 s | $0.0000 | 116 |
| Rules → LLM fallback | 40.6 ms | 3.6 s | $0.0000 | 14 |

**Why answers were wrong**

| Failure | Rules | LLM | Rules → LLM fallback |
|---|---:|---:|---:|
| `wrong_value` | 13 | 37 | 15 |
| `wrong_category` | 4 | 0 | 4 |
| `no_parse` | 6 | 0 | 0 |
| `empty_result` | 0 | 5 | 0 |
| `sql_error` | 0 | 5 | 0 |
| `answered_unanswerable` | 1 | 0 | 1 |

**What this shows**

- **The rules are precise inside their 12 categories (95%) and blind outside
  them (1 of 20).** The 3B model is the reverse: 52% inside, but 75% on the
  questions no category covers, and it declined all 9 unanswerable ones,
  including "delete all of Curry's games".
- **The fallback is right when it runs (12 of 14), but it rarely runs.** It
  only fires when the rules can't parse a question or their handler errors.
  The rules answered 18 questions confidently wrong, usually by matching a
  category's keywords and answering a nearby question ("30+ points with zero
  turnovers" gets a count of all 30-point games). The LLM got 12 of those
  right but never saw them. Choosing the right approach per question would
  score 108/116 (93%) instead of 83%, so the next gain is in **when** to fall
  back, for example when the parse ignores words in the question, not in a
  bigger model.
- **The small model's misses are ones a larger model should mostly avoid:**
  summing a stat over the 12-player `games` table instead of the league table,
  dividing two players' averages instead of listing them, and querying a
  column that doesn't exist.
- Three LLM misses are arguably right: for "Curry's last 10 games" and
  "Embiid's last 5 games" it returned the game logs rather than averages, and
  for "all-time leaders in blocks" only the leader rather than the top 3.
  They're counted wrong, since the gold answers expect the summary.

## Project layout

| Path | What it is |
|---|---|
| `server.py`, `web/index.html` | local web app and its JSON API (`/api/ask?q=...`) |
| `ask.py` | terminal interface |
| `engine/parser.py` | question → category + params |
| `engine/vocab.py` | team names, stat synonyms, player nicknames, `CURRENT_SEASON` |
| `engine/handlers.py` | the twelve categories |
| `engine/fetch.py` | on-demand nba_api fetching and daily refresh |
| `engine/allnba.py`, `all-nba.csv` | All-NBA selections and player birthdates/career spans |
| `seed.py`, `schema.py` | optional pre-loading; database path and `games` table |
| `engine/llm_sql.py` | LLM text-to-SQL: prompt, read-only SQL runner, rules-first `ask_hybrid` |
| `evals/questions.jsonl`, `evals/snapshot.db` | 116-question test set with gold SQL; frozen database it's scored on |
| `evals/run.py`, `evals/scoring.py`, `evals/results/` | eval runner, value-based grading, saved per-question results |
| `tests/test_parser.py` | parser and end-to-end answer tests |
| `tests/test_llm_sql.py` | read-only guards, mocked-model retries/fallback, grading, gold-SQL checks |
| `ARCHITECTURE.md` | current design and the planned discourse engine |

## Tests

```bash
venv/bin/python tests/test_parser.py
```

```bash
venv/bin/python tests/test_llm_sql.py
```

Tests never call nba_api (`COURTSIDE_OFFLINE=1`) or the Anthropic API (the
model is a fake client), so they're fast and run offline. The parser's answer
checks need a seeded `courtside.db`; the LLM and eval tests use the committed
`evals/snapshot.db`.

## Teaching it new words

When a question fails, it's usually a missing word. Add it to
`engine/vocab.py` (a team nickname, stat synonym or player nickname), then add
the question and its expected parse to `PARSE_CASES` in the tests.

When a new season starts, change `CURRENT_SEASON` in `engine/vocab.py`.

## Limits

- Record-chase projections ("will anyone beat...?") only consider active players
  already in the all-time top 25, use their average over the last 3 full
  seasons, and assume they keep that pace.

- Regular season only; no playoffs yet.
- League-wide game questions ("who has the most 40-point games?") only see
  players already in the database. Leaderboards use season totals for every
  player, so they're complete.
- Common words that are also last names ("Brown", "Green", "Young") need the
  player's full name.
- Triple-doubles are counted as 10+ points, rebounds and assists, which misses
  rare ones built on steals or blocks.
