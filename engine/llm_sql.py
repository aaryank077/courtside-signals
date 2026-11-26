"""LLM text-to-SQL: a plain-English question -> one read-only SELECT on courtside.db.

The rule-based parser (engine/parser.py) stays the first choice. This module is
the second path: Claude gets the schema, a page of notes on how the data is
encoded, and a few worked examples, and writes one SQLite query. ask_hybrid()
tries the rules first and only calls the model when they can't answer.

The model's SQL is treated as untrusted input:

- the database is opened read-only (mode=ro) with PRAGMA query_only on
- an authorizer allows only SELECT and reads; writes, PRAGMA, ATTACH and
  everything else are refused before the statement runs
- sqlite3 refuses more than one statement per execute()
- a progress handler aborts any query that runs longer than TIMEOUT_S
- at most MAX_ROWS rows are fetched

If the SQL fails, the error goes back to the model once so it can fix it.
If the second try fails too, the result says so instead of guessing.

The model is Claude through the Anthropic API by default. A model named
"ollama/<name>" (e.g. "ollama/qwen2.5-coder:3b") runs on a local Ollama server
instead: same prompt, same output schema, same SQL guards, no API cost.
"""

import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

MODEL = os.environ.get("COURTSIDE_MODEL", "claude-opus-5-5")
DB_PATH = Path(__file__).resolve().parent.parent / "courtside.db"
TIMEOUT_S = 2.0
MAX_ROWS = 200
MAX_ATTEMPTS = 2  # first try + one repair with the error message

# USD per million tokens: (input, output, cache read). Cache writes are 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
# models that support server-side refusal fallback ("default" picks the model)
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

LOCAL_PREFIX = "ollama/"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


class UnsafeSQL(ValueError):
    """The SQL tried to do something other than read."""


class QueryTimeout(RuntimeError):
    """The SQL ran longer than the time limit."""


# ---------------------------------------------------------------- running SQL safely

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                    getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


def _authorize(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def check_sql(sql):
    """Cheap checks before SQLite sees the query. The authorizer is the real guard;
    this just gives a clearer error for the obvious cases."""
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise UnsafeSQL("empty query")
    first = sql.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        raise UnsafeSQL(f"only SELECT queries are allowed, got {first}")
    return sql


def run_readonly(sql, db_path=DB_PATH, timeout_s=TIMEOUT_S, max_rows=MAX_ROWS):
    """Run one SELECT. Returns (columns, rows, truncated).

    Raises UnsafeSQL, QueryTimeout, or sqlite3.Error (bad SQL)."""
    sql = check_sql(sql)
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorize)
        deadline = time.monotonic() + timeout_s
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            cur = conn.execute(sql)
            rows = cur.fetchmany(max_rows + 1)
        except sqlite3.DatabaseError as e:
            if "not authorized" in str(e):
                raise UnsafeSQL(str(e)) from e
            if "interrupted" in str(e):
                raise QueryTimeout(f"query ran longer than {timeout_s}s") from e
            raise
        columns = [d[0] for d in cur.description or []]
        return columns, [list(r) for r in rows[:max_rows]], len(rows) > max_rows
    finally:
        conn.close()


# ---------------------------------------------------------------- prompt

DAY_KEY = ("printf('%s-%02d-%s', substr(game_date, 9, 4), "
           "(instr('JanFebMarAprMayJunJulAugSepOctNovDec', substr(game_date, 1, 3)) + 2) / 3, "
           "substr(game_date, 5, 2))")

SCHEMA_NOTES = """\
You write one SQLite query that answers a question about NBA stats, using only
the database described here. Regular season only; there is no playoff data.

TABLES

games: one row per player per regular-season game, for ONLY these players:
  {players}
  columns: game_id, player_id, player_name, season_id, game_date, matchup, wl,
  min, fgm, fga, fg_pct, fg3m, fg3a, fg3_pct, ftm, fta, ft_pct, oreb, dreb, reb,
  ast, stl, blk, tov, pf, pts, plus_minus
  - season_id is '2' + the season's start year: '22023' is 2023-24. This season
    (2026-27) is '22026'; last season is '22025'.
  - game_date is text like 'Apr 09, 2019' and does NOT sort by date, and
    game_id is not in date order either (rescheduled games). For chronological
    order use this sortable day key, written here as {{day}}:
    {day}
  - matchup is 'DAL vs. LAL' for a home game and 'DAL @ LAL' for a road game;
    the opponent is substr(matchup, -3). The player's own team is the first 3 letters.
  - wl is 'W' or 'L' for the player's team.
  - player_name has accents: 'Nikola Jokić', 'Luka Dončić'.

career_totals: one row per player above, regular-season career totals
  (player_id, player_name, gp, gs, min, fgm, fga, fg3m, fg3a, ftm, fta, oreb,
  dreb, reb, ast, stl, blk, tov, pf, pts, and *_pct columns).

league_stats: every NBA player THIS season (2026-27) only, one row each, season
  TOTALS: player_name, team_abbreviation, age, gp, w, l, min (total minutes),
  pts, reb, ast, stl, blk, tov, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, pf,
  plus_minus, dd2 (double-doubles), td3 (triple-doubles), *_pct columns.

all_time_leaders: career top 25 for each stat: stat, rank, player_id,
  player_name, value, active (1 = still playing). stat is one of pts, reb, ast,
  stl, blk, tov, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, pf, gp, fg_pct,
  fg3_pct, ft_pct. Percentages are fractions (0.91).

all_nba: All-NBA selections: season ('2015-16'), start_year (2015), team
  ('1st', '2nd', '3rd'), player_name, player_id.
player_bio: player_id, player_name, birthdate ('1984-12-30'), from_year, to_year.
  Age at a selection is measured on Feb 1 of the season:
  (julianday((start_year + 1) || '-02-01') - julianday(birthdate)) / 365.25

CONVENTIONS
- "Per game" and averages: AVG over the player's games. Percentages: total made
  / total attempted (1.0 * SUM(fgm) / SUM(fga)), never AVG of a pct column.
- A triple-double is pts >= 10 AND reb >= 10 AND ast >= 10.
- "30-point games" means pts >= 30. Thresholds are inclusive.
- "Lately" / "recently" with no number means the last 5 games.
- A streak is consecutive games in the player's game log, in {{day}} order.
- Per-game league leaderboards only count players with at least half the games
  of the player with the most: gp >= (SELECT (MAX(gp) + 1) / 2 FROM league_stats),
  unless the question sets its own minimum. Season totals need no minimum.
- "Record" means wins and losses: SUM(wl = 'W'), SUM(wl = 'L').
- A "career" question about a player uses career_totals; "all-time" rank
  questions use all_time_leaders.

OUTPUT
- Return the columns that answer the question, with labels next to values
  (player_name, season). Order rows so the answer comes first, and LIMIT lists.
- Show seasons as '2015-16':
  printf('%d-%02d', CAST(substr(season_id, 2) AS INT), (CAST(substr(season_id, 2) AS INT) + 1) % 100)
- Round averages to 1 decimal; leave percentages as fractions rounded to 3.
- If the database can't answer (playoffs, salaries, players or seasons not in
  it, opinions, predictions, anything that would change data), set sql to ""
  and say why in cannot_answer. Otherwise set cannot_answer to "".
"""

EXAMPLES = [
    ("How many 35-point games does Embiid have against the Knicks?",
     "SELECT COUNT(*) AS games FROM games WHERE player_name = 'Joel Embiid' "
     "AND pts >= 35 AND substr(matchup, -3) = 'NYK'"),
    ("Paul George's free throw percentage in 2018-19",
     "SELECT ROUND(1.0 * SUM(ftm) / SUM(fta), 3) AS ft_pct FROM games "
     "WHERE player_name = 'Paul George' AND season_id = '22018'"),
    ("Curry's longest streak of 25+ point games",
     f"WITH g AS (SELECT game_id, pts >= 25 AS hit, ROW_NUMBER() OVER (ORDER BY {DAY_KEY}) AS rn "
     "FROM games WHERE player_name = 'Stephen Curry'), "
     "runs AS (SELECT rn - ROW_NUMBER() OVER (ORDER BY rn) AS grp FROM g WHERE hit) "
     "SELECT COUNT(*) AS longest FROM runs GROUP BY grp ORDER BY longest DESC LIMIT 1"),
    ("Who leads the league in rebounds per game?",
     "SELECT player_name, team_abbreviation, gp, ROUND(1.0 * reb / gp, 1) AS rpg "
     "FROM league_stats WHERE gp >= (SELECT (MAX(gp) + 1) / 2 FROM league_stats) "
     "ORDER BY 1.0 * reb / gp DESC LIMIT 5"),
    ("Murray's averages over his last 7 games",
     "SELECT ROUND(AVG(pts), 1) AS pts, ROUND(AVG(reb), 1) AS reb, ROUND(AVG(ast), 1) AS ast "
     f"FROM (SELECT * FROM games WHERE player_name = 'Jamal Murray' ORDER BY {DAY_KEY} DESC LIMIT 7)"),
    ("How many All-NBA second team selections does Curry have?",
     "SELECT COUNT(*) AS selections FROM all_nba WHERE player_name = 'Stephen Curry' AND team = '2nd'"),
]
CANNOT_EXAMPLE = ("What was Jokic's plus-minus in the 2023 Finals?",
                  "There is no playoff data, only regular-season games.")

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "cannot_answer": {"type": "string"},
    },
    "required": ["sql", "cannot_answer"],
    "additionalProperties": False,
}


def _players_in(db_path):
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT player_name FROM games ORDER BY player_name")]
    finally:
        conn.close()


def system_prompt(db_path=DB_PATH):
    """Schema notes + worked examples. Stable for a given database, so it caches."""
    text = SCHEMA_NOTES.format(players=", ".join(_players_in(db_path)), day=DAY_KEY)
    shots = [f"Q: {q}\n{json.dumps({'sql': s, 'cannot_answer': ''}, ensure_ascii=False)}"
             for q, s in EXAMPLES]
    q, why = CANNOT_EXAMPLE
    shots.append(f"Q: {q}\n{json.dumps({'sql': '', 'cannot_answer': why})}")
    return text + "\nEXAMPLES\n\n" + "\n\n".join(shots)


# ---------------------------------------------------------------- calling the model

def cost_usd(model, usage):
    """Dollar cost of one response from its usage counts (0 for unknown models)."""
    if model not in PRICES:
        return 0.0
    inp, out, cached = PRICES[model]
    return (usage.get("input_tokens", 0) * inp
            + usage.get("cache_creation_input_tokens", 0) * inp * 1.25
            + usage.get("cache_read_input_tokens", 0) * cached
            + usage.get("output_tokens", 0) * out) / 1e6


def _usage_dict(usage):
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens")
    return {k: int(getattr(usage, k, 0) or 0) for k in keys}


def _parse(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"sql": "", "cannot_answer": "", "_bad_json": text[:200]}


def _ollama_post(path, body, timeout=600):
    req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _call_local(name, system, messages):
    """One request to a local Ollama model, constrained to the same JSON schema."""
    data = _ollama_post("/api/chat", {
        "model": name,
        "messages": [{"role": "system", "content": system}, *messages],
        "format": OUTPUT_SCHEMA,
        "stream": False,
        "options": {"temperature": 0, "num_ctx": 4096},
    })
    usage = {"input_tokens": int(data.get("prompt_eval_count") or 0),
             "output_tokens": int(data.get("eval_count") or 0),
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    return _parse(data.get("message", {}).get("content", "")), usage


def _call(client, model, system, messages):
    """One request. Returns (parsed JSON dict, usage dict)."""
    if model.startswith(LOCAL_PREFIX):
        return _call_local(model[len(LOCAL_PREFIX):], system, messages)
    kwargs = dict(
        model=model,
        max_tokens=8000,
        system=system,
        messages=messages,
        cache_control={"type": "ephemeral"},
        output_config={"effort": "low",
                       "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    if model in FALLBACK_MODELS:
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    response = client.beta.messages.create(**kwargs)
    usage = _usage_dict(response.usage)
    if response.stop_reason == "refusal":
        return {"sql": "", "cannot_answer": "the model declined to answer"}, usage
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _parse(text), usage


def _default_client():
    import anthropic
    return anthropic.Anthropic()


def answer(question, client=None, model=MODEL, db_path=DB_PATH):
    """Ask the model for SQL, run it read-only, retry once on an error.

    Returns a dict with status ("ok", "cannot_answer", "sql_error", "unsafe",
    "timeout", "api_error"), sql, columns, rows, truncated, reason, attempts,
    usage (summed), cost_usd and latency_s.
    """
    if client is None and not model.startswith(LOCAL_PREFIX):
        client = _default_client()
    started = time.perf_counter()
    system = system_prompt(db_path)
    messages = [{"role": "user", "content": question}]
    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    result = {"status": "sql_error", "sql": "", "columns": [], "rows": [],
              "truncated": False, "reason": "", "attempts": 0}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result["attempts"] = attempt
        try:
            out, usage = _call(client, model, system, messages)
        except Exception as e:  # network, auth, rate limit after SDK retries
            result.update(status="api_error", reason=f"{type(e).__name__}: {e}")
            break
        for k in total:
            total[k] += usage.get(k, 0)

        sql = (out.get("sql") or "").strip()
        if not sql:
            if out.get("_bad_json"):
                result.update(status="sql_error", reason="model did not return JSON")
            else:
                result.update(status="cannot_answer",
                              reason=out.get("cannot_answer") or "no query")
            break

        result["sql"] = sql
        try:
            columns, rows, truncated = run_readonly(sql, db_path)
            result.update(status="ok", columns=columns, rows=rows,
                          truncated=truncated, reason="")
            break
        except UnsafeSQL as e:
            # never give the model a second shot at a write
            result.update(status="unsafe", reason=str(e))
            break
        except QueryTimeout as e:
            error, status = str(e), "timeout"
        except sqlite3.Error as e:
            error, status = f"{type(e).__name__}: {e}", "sql_error"
        result.update(status=status, reason=error)
        messages += [
            {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
            {"role": "user", "content": f"That query failed with: {error}\n"
                                        "Fix it and return the corrected JSON."},
        ]

    result.update(usage=total, cost_usd=cost_usd(model, total),
                  latency_s=time.perf_counter() - started, model=model)
    return result


def ask_hybrid(question, client=None, model=MODEL, db_path=DB_PATH):
    """Rules first; the LLM only when the parser can't parse it or the handler fails.

    Returns {"source": "rules", **rules_result} or {"source": "llm", **llm_result}.
    """
    from engine.parser import ask
    rules = ask(question)
    if rules["category"] != "unknown" and not rules.get("error"):
        return {"source": "rules", **rules}
    return {"source": "llm", **answer(question, client=client, model=model, db_path=db_path)}
