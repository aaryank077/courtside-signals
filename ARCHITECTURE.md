# Architecture

## Today

```
question ─► engine/parser.py ─► (category, params) ─► engine/handlers.py ─► SQL on courtside.db ─► answer + table
```

- **Data layer**: `courtside.db` (SQLite), filled on demand from nba_api, plus
  All-NBA selections from Basketball Reference (`all-nba.csv`). Game logs and
  career totals are fetched the first time a player is asked about; league
  stats refresh daily.
- **Query engine**: a rule-based parser (word lists in `engine/vocab.py`)
  maps a question to one of twelve categories and a params dict. One handler
  per category turns that into SQL and returns `{answer, table, data}`.
- **LLM fallback** (`engine/llm_sql.py`): when the parser returns `unknown` or
  a handler fails, Claude writes one SELECT from the schema notes and examples.
  It runs on a read-only connection with an authorizer, a timeout and a row
  cap; a failed query is retried once with the error.
  The model is Claude by default; `ollama/<name>` runs a local model through
  Ollama with the same prompt, schema and guards.

```
question ─► parser ─► handler ─► answer
              │ unknown / handler error
              ▼
           Claude ─► SQL ─► read-only SQLite (authorizer, 2 s, 200 rows) ─► rows
                     ▲                    │ error
                     └──── one retry ─────┘
```

- **Evaluation** (`evals/`): 116 questions with gold SQL, scored by result
  values against a frozen snapshot of the database, for rules alone, LLM alone
  and rules-then-LLM. Results and how to run it are in the README.
- **Frontend**: `server.py` serves `web/index.html` and a JSON API
  (`/api/ask?q=...`); `ask.py` is the terminal interface.

## Discourse engine

Reads fan discourse and turns it into answerable stat questions.

```
ESPN/Reddit ─► engine/discourse.py ─► questions ─► [query engine] ─► courtside.db ─► answer ─► [frontend]
```

- **Sources**: Reddit r/nba via PRAW (set `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET`),
  falling back to the ESPN news API (no auth, always works).
- **Theme detection** (`_THEMES` in `discourse.py`): pattern-based mapping of abstract
  discourse to answerable questions, e.g. "medical advances / longevity" →
  "Average career length of All-NBA players over time".
- **Entity templates**: named players/teams/stats → comparison, averages, streak,
  career, record, recent-games questions.
- **Clustering**: two-pass — collect all candidates across posts, then group by question
  text. Questions mentioned across multiple posts surface first (`mentions` field).
  Question generation stays rule-based; no model needed.
- **Frontend**: "Trending on r/nba" pill row in `web/index.html`, loaded async via
  `/api/signals` (cached 15 min in `server.py`).

## Failure logging

Questions the parser can't handle are written to `failed_queries.jsonl` (gitignored)
with a timestamp, category, and the raw query. Review it to decide which words or
categories to add next:

```bash
cat failed_queries.jsonl | python3 -c "import sys,json; [print(json.loads(l)['query']) for l in sys.stdin]"
```

## What's next

- Promote high-signal themes to the examples bar (currently static in `web/index.html`).
- Use `failed_queries.jsonl` to find the most common unparseable patterns and add
  categories or vocab entries for them.
- For Reddit: fetch top comments on the highest-scoring threads (pass
  `fetch_comments=True` to `fetch_signals`) for richer theme signal.
