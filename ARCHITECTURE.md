# Architecture

## Today

```
question ─► engine/parser.py ─► (category, params) ─► engine/handlers.py ─► SQL on courtside.db ─► answer + table
```

- **Data layer**: `courtside.db` (SQLite), filled on demand from nba_api, plus
  All-NBA selections from Basketball Reference (`all-nba.csv`). Game logs and
  career totals are fetched the first time a player is asked about; league
  stats refresh daily.
- **Query engine**: a rule-based parser (word lists in `engine/vocab.py`, no
  LLM) maps a question to one of twelve categories and a params dict. One
  handler per category turns that into SQL and returns `{answer, table, data}`.
- **Frontend**: `server.py` serves `web/index.html` and a JSON API
  (`/api/ask?q=...`); `ask.py` is the terminal interface.

## Next: discourse engine

The plan is to read what fans are actually arguing about (e.g. Reddit threads)
and turn it into questions the query engine can answer. If fans are saying
superstars are older than they used to be, it should generate "what's the
average age of an All-NBA player over time?" and answer it with data.

```
Reddit ─► [discourse engine] ─► questions ─► [query engine] ─► courtside.db ─► answer ─► [frontend]
```

Open questions:

- How to cluster threads into topics and pick which ones are answerable.
- Whether question generation needs a model or can stay rule-based like the parser.
- Logging questions the parser fails on (`failed_queries`) to decide which
  categories to add next.
