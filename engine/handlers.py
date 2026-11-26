"""Query handlers for the five intent categories.

Each handler takes a dict of structured params (the LLM intent classifier will
fill these in later) and returns a dict:

    {"category": str, "answer": str, "table": DataFrame | None, "data": dict}

Example:
    follow_query("game_count", {"player": "Luka Doncic", "stat": "pts",
                                "threshold": 30, "opponent": "LAL"})

Intent categories: best_season, game_count, player_averages, league_leaders,
career_stat.
"""

import datetime
import json
import math
import sqlite3
import unicodedata
from pathlib import Path

import pandas as pd
from nba_api.stats.static import players

from engine.fetch import (birthdate, ensure_all_time_leaders, ensure_league_stats,
                          ensure_player, season_totals)
from engine.vocab import CURRENT_SEASON

DB_PATH = Path(__file__).resolve().parent.parent / "courtside.db"
_FAILED_PATH = Path(__file__).resolve().parent.parent / "failed_queries.jsonl"

failed_queries = []


def _log_failed(category, query):
    """Append to in-memory list and persist to failed_queries.jsonl."""
    failed_queries.append((category, query))
    entry = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "category": category,
        "query": query if isinstance(query, str) else query,
    }
    try:
        with open(_FAILED_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass

# Stats that are summed over games, and percentages computed from them.
# Column names can't be SQL parameters, so every stat is checked against these.
COUNTING_STATS = ["pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga",
                  "fg3m", "fg3a", "ftm", "fta", "oreb", "dreb", "pf", "min"]
GAME_ONLY_STATS = ["plus_minus"]
PCT_STATS = {"fg_pct": ("fgm", "fga"),
             "fg3_pct": ("fg3m", "fg3a"),
             "ft_pct": ("ftm", "fta")}

STAT_LABELS = {"pts": "PPG", "reb": "RPG", "ast": "APG", "stl": "SPG",
               "blk": "BPG", "tov": "TOV", "min": "MPG", "fg_pct": "FG%",
               "fg3_pct": "3P%", "ft_pct": "FT%", "fg3m": "3PM", "fg3a": "3PA",
               "plus_minus": "+/-"}


class QueryError(ValueError):
    """Raised when params are missing or invalid, or nothing matches."""


# ---------------------------------------------------------------- helpers

def _connect():
    return sqlite3.connect(DB_PATH)


def _normalize(name):
    stripped = unicodedata.normalize("NFKD", name)
    return "".join(c for c in stripped if not unicodedata.combining(c)).lower().strip()


def _resolve_player(name):
    """Turn a typed name ("luka doncic", "Jokic") into an nba_api player dict.

    Exact full-name matches win; otherwise a unique partial match is used, and
    among several partial matches, active players win.
    """
    if not name:
        raise QueryError("A player name is required.")
    target = _normalize(name)
    all_players = players.get_players()

    exact = [p for p in all_players if _normalize(p["full_name"]) == target]
    if exact:
        return exact[0]

    partial = [p for p in all_players if target in _normalize(p["full_name"])]
    if len(partial) > 1:
        partial = [p for p in partial if p["is_active"]] or partial
    if len(partial) > 1:
        with _connect() as conn:
            seeded = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM games")}
        partial = [p for p in partial if p["id"] in seeded] or partial
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise QueryError(f"No player found matching '{name}'.")
    names = ", ".join(p["full_name"] for p in partial[:5])
    raise QueryError(f"'{name}' matches several players: {names}.")


def _check_stat(stat, allowed):
    if stat not in allowed:
        raise QueryError(f"Unknown stat '{stat}'. Choose from: {', '.join(allowed)}.")
    return stat


def _season_label(season_id):
    """'22018' (nba_api season id) -> '2018-19'."""
    year = int(str(season_id)[1:])
    return f"{year}-{str(year + 1)[-2:]}"


def _season_id(label):
    """'2018-19' -> '22018' (regular season)."""
    try:
        return f"2{int(label[:4])}"
    except (TypeError, ValueError):
        raise QueryError(f"Season should look like '2023-24', got '{label}'.")


def _game_filters(params):
    """WHERE clauses shared by the game-log handlers."""
    player = _resolve_player(params.get("player"))
    with _connect() as conn:
        ensure_player(conn, player)
    clauses, args = ["player_id = ?"], [player["id"]]

    if params.get("opponent"):
        # matchup looks like "DAL vs. LAL" (home) or "DAL @ LAL" (away)
        clauses.append("substr(matchup, -3) = ?")
        args.append(params["opponent"].upper())
    if params.get("season"):
        clauses.append("season_id = ?")
        args.append(_season_id(params["season"]))
    home_away = params.get("home_away")
    if home_away == "home":
        clauses.append("matchup LIKE '% vs. %'")
    elif home_away == "away":
        clauses.append("matchup LIKE '% @ %'")
    elif home_away:
        raise QueryError("home_away should be 'home' or 'away'.")

    return player, " AND ".join(clauses), args


def _describe_filters(params):
    parts = []
    if params.get("opponent"):
        parts.append(f"vs {params['opponent'].upper()}")
    if params.get("season"):
        parts.append(f"in {params['season']}")
    if params.get("home_away"):
        parts.append("at home" if params["home_away"] == "home" else "on the road")
    return (" " + " ".join(parts)) if parts else ""


def _fmt(stat, value):
    if pd.isna(value):
        return "n/a"
    if stat in PCT_STATS:
        return f"{value:.1%}"
    return f"{value:.1f}"


def _add_pcts(df):
    for pct, (made, att) in PCT_STATS.items():
        df[pct] = (df[made] / df[att].where(df[att] > 0)).round(3)
    return df


def _apply_minimums(df, minimums):
    """Keep rows meeting every {stat: minimum} in params['min']."""
    for stat, minimum in (minimums or {}).items():
        _check_stat(stat, df.columns.tolist())
        df = df[df[stat] >= minimum]
    return df


# ---------------------------------------------------------------- handlers

def best_season(params):
    """Player's best season by one stat.

    params: player, stat (default "pts"), min ({stat: season total}, e.g.
    {"fg3a": 400}), min_games, ascending (True ranks lowest first, e.g. tov).
    Counting stats are ranked per game; percentages are made / attempted.
    """
    stat = _check_stat(params.get("stat", "pts"), COUNTING_STATS + list(PCT_STATS))
    player, where, args = _game_filters({"player": params.get("player")})

    sums = ", ".join(f"SUM({s}) AS {s}" for s in COUNTING_STATS)
    with _connect() as conn:
        df = pd.read_sql(
            f"SELECT season_id, COUNT(*) AS gp, {sums} FROM games "
            f"WHERE {where} GROUP BY season_id", conn, params=args)
    if df.empty:
        raise QueryError(f"No games in the database for {player['full_name']}.")

    df = _add_pcts(df)
    df = _apply_minimums(df, params.get("min"))
    if params.get("min_games"):
        df = df[df["gp"] >= params["min_games"]]
    if df.empty:
        raise QueryError(f"No {player['full_name']} season meets those minimums.")

    rank_col = stat if stat in PCT_STATS else f"{stat}_pg"
    if stat not in PCT_STATS:
        df[rank_col] = (df[stat] / df["gp"]).round(1)
    df = df.sort_values(rank_col, ascending=bool(params.get("ascending")))
    df.insert(0, "season", df.pop("season_id").map(_season_label))

    best = df.iloc[0]
    label = STAT_LABELS.get(stat, stat.upper())
    minimums = [f"{v}+ {k}" for k, v in (params.get("min") or {}).items()]
    if params.get("min_games"):
        minimums.append(f"{params['min_games']}+ games")
    qualifier = f" (min {', '.join(minimums)})" if minimums else ""
    answer = (f"{player['full_name']}'s best season by {label}{qualifier} was "
              f"{best['season']}: {_fmt(stat, best[rank_col])}")
    if stat in PCT_STATS:
        made, att = PCT_STATS[stat]
        answer += f" ({int(best[made])}/{int(best[att])})"
    answer += f" in {int(best['gp'])} games."

    columns = ["season", "gp", rank_col] + ([*PCT_STATS[stat]] if stat in PCT_STATS else [stat])
    return {"answer": answer,
            "table": df[columns].reset_index(drop=True),
            "data": {"season": best["season"], "value": best[rank_col]}}


def game_count(params):
    """Count a player's games meeting stat thresholds.

    params: player, and either stat + threshold (e.g. "pts", 30) or
    conditions ({"pts": 30, "reb": 10}), each meaning stat >= value.
    Optional filters: opponent (team abbreviation), season, home_away.
    """
    conditions = dict(params.get("conditions") or {})
    if params.get("stat") is not None:
        if params.get("threshold") is None:
            raise QueryError("game_count needs a threshold for the stat.")
        conditions[params["stat"]] = params["threshold"]
    if not conditions:
        raise QueryError("game_count needs a stat and threshold (or conditions).")

    player, where, args = _game_filters(params)
    base_where, base_args = where, list(args)
    for stat, threshold in conditions.items():
        _check_stat(stat, COUNTING_STATS + GAME_ONLY_STATS)
        where += f" AND {stat} >= ?"
        args.append(threshold)

    shown = list(dict.fromkeys(["pts", "reb", "ast", *conditions]))
    with _connect() as conn:
        df = pd.read_sql(
            f"SELECT game_date, season_id, matchup, wl, {', '.join(shown)} "
            f"FROM games WHERE {where} ORDER BY game_date", conn, params=args)
        total = conn.execute(
            f"SELECT COUNT(*) FROM games WHERE {base_where}", base_args).fetchone()[0]

    df["game_date"] = pd.to_datetime(df["game_date"], format="mixed")
    df = df.sort_values("game_date").reset_index(drop=True)
    df["game_date"] = df["game_date"].dt.strftime("%Y-%m-%d")
    df.insert(1, "season", df.pop("season_id").map(_season_label))

    cond_text = " and ".join(f"{v}+ {k}" for k, v in conditions.items())
    answer = (f"{player['full_name']} has {len(df)} games with {cond_text}"
              f"{_describe_filters(params)} (out of {total} games in the database).")
    return {"answer": answer, "table": df, "data": {"count": len(df), "games": total}}


def player_averages(params):
    """A player's per-game averages, with the matching game log.

    params: player, stats (list, default ["pts", "reb", "ast"]), and optional
    filters opponent, season, home_away.
    """
    stats = params.get("stats") or ["pts", "reb", "ast"]
    for stat in stats:
        _check_stat(stat, COUNTING_STATS + GAME_ONLY_STATS + list(PCT_STATS))

    player, where, args = _game_filters(params)
    cols = COUNTING_STATS + GAME_ONLY_STATS
    with _connect() as conn:
        df = pd.read_sql(
            f"SELECT game_date, season_id, matchup, wl, {', '.join(cols)} "
            f"FROM games WHERE {where}", conn, params=args)
    if df.empty:
        raise QueryError(f"No {player['full_name']} games{_describe_filters(params)} "
                         "in the database.")

    totals = df[COUNTING_STATS].sum()
    averages = {}
    for stat in stats:
        if stat in PCT_STATS:
            made, att = PCT_STATS[stat]
            averages[stat] = totals[made] / totals[att] if totals[att] else float("nan")
        else:
            averages[stat] = df[stat].mean()

    df["game_date"] = pd.to_datetime(df["game_date"], format="mixed")
    df = df.sort_values("game_date").reset_index(drop=True)
    df["game_date"] = df["game_date"].dt.strftime("%Y-%m-%d")
    df.insert(1, "season", df.pop("season_id").map(_season_label))
    shown = [s for s in stats if s not in PCT_STATS] or ["pts"]

    summary = ", ".join(f"{_fmt(s, v)} {STAT_LABELS.get(s, s.upper())}"
                        for s, v in averages.items())
    answer = (f"{player['full_name']}{_describe_filters(params)}: {summary} "
              f"over {len(df)} games.")
    return {"answer": answer,
            "table": df[["game_date", "season", "matchup", "wl", *shown]],
            "data": {"games": len(df),
                     "averages": {s: round(float(v), 3) for s, v in averages.items()}}}


def best_games(params):
    """A player's top single games by one stat ("career high", "most points in a game").

    params: player, stat (default "pts"), limit (default 5), ascending, and
    optional filters opponent, season, home_away. Only covers games in the
    database, which may not be the player's whole career.
    """
    stat = _check_stat(params.get("stat", "pts"), COUNTING_STATS + GAME_ONLY_STATS)
    player, where, args = _game_filters(params)
    shown = list(dict.fromkeys([stat, "pts", "reb", "ast"]))
    order = "ASC" if params.get("ascending") else "DESC"
    limit = int(params.get("limit", 5))

    with _connect() as conn:
        df = pd.read_sql(
            f"SELECT game_date, season_id, matchup, wl, {', '.join(shown)} "
            f"FROM games WHERE {where} ORDER BY {stat} {order}, game_date",
            conn, params=args)
    if df.empty:
        raise QueryError(f"No {player['full_name']} games{_describe_filters(params)} "
                         "in the database.")

    first_season = _season_label(df["season_id"].min())
    df.insert(1, "season", df.pop("season_id").map(_season_label))
    df["game_date"] = pd.to_datetime(df["game_date"], format="mixed").dt.strftime("%Y-%m-%d")
    top = df.iloc[0]
    tied = int((df[stat] == top[stat]).sum())

    value = int(top[stat]) if float(top[stat]).is_integer() else top[stat]
    word = "lowest" if params.get("ascending") else "most"
    answer = (f"{player['full_name']}'s {word} {stat} in a game{_describe_filters(params)} "
              f"is {value}, on {top['game_date']} ({top['matchup']})")
    if tied > 1:
        answer += f", tied across {tied} games"
    answer += f". Based on {len(df)} games since {first_season} in the database."
    return {"answer": answer, "table": df.head(limit).reset_index(drop=True),
            "data": {"value": float(top[stat]), "game_date": top["game_date"]}}


def _player_games(params):
    """All of a player's games matching the filters, oldest first, with a
    readable season and date. Returns (player, DataFrame)."""
    player, where, args = _game_filters(params)
    cols = COUNTING_STATS + GAME_ONLY_STATS
    with _connect() as conn:
        df = pd.read_sql(
            f"SELECT game_date, season_id, matchup, wl, {', '.join(cols)} "
            f"FROM games WHERE {where}", conn, params=args)
    if df.empty:
        raise QueryError(f"No {player['full_name']} games{_describe_filters(params)} "
                         "in the database.")
    df["game_date"] = pd.to_datetime(df["game_date"], format="mixed")
    df = df.sort_values("game_date").reset_index(drop=True)
    df["game_date"] = df["game_date"].dt.strftime("%Y-%m-%d")
    df.insert(1, "season", df.pop("season_id").map(_season_label))
    return player, df


def _averages(df, stats):
    """Per-game averages; percentages are total made / total attempted."""
    out = {}
    for stat in stats:
        if stat in PCT_STATS:
            made, att = PCT_STATS[stat]
            total = df[att].sum()
            out[stat] = df[made].sum() / total if total else float("nan")
        else:
            out[stat] = df[stat].mean()
    return out


def _conditions(params, required=True):
    """{stat: minimum} from params['conditions'] or stat + threshold."""
    conditions = dict(params.get("conditions") or {})
    if params.get("stat") is not None and params.get("threshold") is not None:
        conditions[params["stat"]] = params["threshold"]
    for stat in conditions:
        _check_stat(stat, COUNTING_STATS + GAME_ONLY_STATS)
    if required and not conditions:
        raise QueryError("This needs a stat and threshold, like 30+ points.")
    return conditions


def _matches(df, conditions):
    mask = pd.Series(True, index=df.index)
    for stat, minimum in conditions.items():
        mask &= df[stat] >= minimum
    return mask


def _cond_text(conditions):
    return " and ".join(f"{v}+ {k}" for k, v in conditions.items())


def compare_players(params):
    """Two or more players' per-game averages side by side.

    params: players (list of names), stats (default ["pts", "reb", "ast"]),
    optional filters opponent, season, home_away (applied to every player).
    """
    names = params.get("players") or []
    if len(names) < 2:
        raise QueryError("Name at least two players to compare.")
    stats = params.get("stats") or ["pts", "reb", "ast"]
    for stat in stats:
        _check_stat(stat, COUNTING_STATS + GAME_ONLY_STATS + list(PCT_STATS))

    rows = []
    for name in names:
        player, df = _player_games({**params, "player": name})
        rows.append({"player": player["full_name"], "gp": len(df),
                     **{s: round(float(v), 3 if s in PCT_STATS else 1)
                        for s, v in _averages(df, stats).items()}})
    table = pd.DataFrame(rows)

    parts = []
    for stat in stats:
        ranked = table.sort_values(stat, ascending=(stat == "tov"))
        values = ", ".join(f"{r['player'].split()[-1]} {_fmt(stat, r[stat])}"
                           for _, r in ranked.iterrows())
        parts.append(f"{STAT_LABELS.get(stat, stat.upper())}: {values}")
    scope = _describe_filters(params).strip() or "career"
    answer = "; ".join(parts) + f" ({scope}, regular season)."
    return {"answer": answer, "table": table,
            "data": {"rows": table.to_dict("records")}}


def record(params):
    """A player's team win-loss record, optionally in games meeting conditions.

    params: player, optional conditions / stat + threshold ("when he scores
    40+"), and filters opponent, season, home_away.
    """
    conditions = _conditions(params, required=False)
    player, df = _player_games(params)
    df = df[_matches(df, conditions)]
    if df.empty:
        raise QueryError(f"{player['full_name']} has no games with "
                         f"{_cond_text(conditions)}{_describe_filters(params)}.")

    by_season = (df.assign(w=df["wl"].eq("W"), l=df["wl"].eq("L"))
                   .groupby("season", as_index=False)[["w", "l"]].sum())
    by_season["win_pct"] = (by_season["w"] / (by_season["w"] + by_season["l"])).round(3)
    wins, losses = int(by_season["w"].sum()), int(by_season["l"].sum())
    pct = wins / (wins + losses) if wins + losses else float("nan")

    when = f" in games with {_cond_text(conditions)}" if conditions else ""
    answer = (f"{player['full_name']}'s teams are {wins}-{losses} "
              f"({f'{pct:.3f}'.lstrip('0')}){when}"
              f"{_describe_filters(params)}.")
    return {"answer": answer, "table": by_season,
            "data": {"wins": wins, "losses": losses, "win_pct": pct}}


def streak(params):
    """A player's longest run of consecutive games meeting conditions.

    params: player, conditions or stat + threshold, filters opponent, season,
    home_away. Consecutive means games he played, back to back in the database.
    """
    conditions = _conditions(params)
    player, df = _player_games(params)
    hit = _matches(df, conditions)

    run_id = (hit != hit.shift()).cumsum()
    runs = df[hit].groupby(run_id[hit])
    if runs.ngroups == 0:
        return {"answer": f"{player['full_name']} has no games with "
                          f"{_cond_text(conditions)}{_describe_filters(params)} in the database.",
                "table": None, "data": {"longest": 0, "current": 0}}

    longest = max(runs, key=lambda g: len(g[1]))[1]
    current = 0
    for value in reversed(hit.tolist()):
        if not value:
            break
        current += 1

    shown = list(dict.fromkeys(["pts", "reb", "ast", *conditions]))
    answer = (f"{player['full_name']}'s longest streak of games with "
              f"{_cond_text(conditions)}{_describe_filters(params)} is {len(longest)}, "
              f"from {longest['game_date'].iloc[0]} to {longest['game_date'].iloc[-1]}. "
              f"Current streak: {current}.")
    return {"answer": answer,
            "table": longest[["game_date", "season", "matchup", "wl", *shown]].reset_index(drop=True),
            "data": {"longest": len(longest), "current": current}}


def recent_games(params):
    """A player's last N games and his averages over them.

    params: player, n (default 10), stats (default ["pts", "reb", "ast"]),
    filters opponent, season, home_away.
    """
    n = int(params.get("n", 10))
    stats = params.get("stats") or ["pts", "reb", "ast"]
    for stat in stats:
        _check_stat(stat, COUNTING_STATS + GAME_ONLY_STATS + list(PCT_STATS))

    player, df = _player_games(params)
    last = df.tail(n)
    averages = _averages(last, stats)
    summary = ", ".join(f"{_fmt(s, v)} {STAT_LABELS.get(s, s.upper())}"
                        for s, v in averages.items())
    wins = int(last["wl"].eq("W").sum())

    answer = (f"{player['full_name']}'s last {len(last)} games{_describe_filters(params)} "
              f"({last['game_date'].iloc[0]} to {last['game_date'].iloc[-1]}): {summary}, "
              f"{wins}-{len(last) - wins} record.")
    shown = [s for s in stats if s not in PCT_STATS] or ["pts"]
    return {"answer": answer,
            "table": last[["game_date", "season", "matchup", "wl", *shown]]
                     .iloc[::-1].reset_index(drop=True),
            "data": {"games": len(last),
                     "averages": {s: round(float(v), 3) for s, v in averages.items()}}}


def league_leaders(params):
    """League leaders this season (league_stats table).

    params: stat (default "pts"), per ("game" default, or "total"),
    min_minutes (minutes per game), min_games (default: half the most games
    anyone has played; 0 turns it off), min ({stat: season total}),
    team (abbreviation), limit (default 10).
    """
    stat = _check_stat(params.get("stat", "pts"),
                       COUNTING_STATS + list(PCT_STATS) + ["plus_minus"])
    per = params.get("per", "game")
    if per not in ("game", "total"):
        raise QueryError("per should be 'game' or 'total'.")

    with _connect() as conn:
        ensure_league_stats(conn)
        try:
            df = pd.read_sql("SELECT * FROM league_stats", conn)
        except pd.errors.DatabaseError:
            raise QueryError("League stats aren't loaded, and nba_api couldn't be reached.")

    loaded = df["season"].iloc[0] if "season" in df and len(df) else None
    if params.get("season") and loaded and params["season"] != loaded:
        raise QueryError(f"League leaders are only loaded for {loaded}, "
                         f"not {params['season']}.")

    df = df[df["gp"] > 0].copy()
    df["mpg"] = df["min"] / df["gp"]
    if params.get("team"):
        df = df[df["team_abbreviation"] == params["team"].upper()]
    if params.get("min_minutes"):
        df = df[df["mpg"] >= params["min_minutes"]]
    # Unless asked otherwise, require half the games the busiest player has
    # played, so someone with 3 great games doesn't top a per-game list.
    min_games = params.get("min_games")
    if min_games is None and per == "game":
        min_games = math.ceil(df["gp"].max() * 0.5)
    if min_games:
        df = df[df["gp"] >= min_games]
    df = _apply_minimums(df, params.get("min"))
    if df.empty:
        raise QueryError("No players meet those filters.")

    if stat in PCT_STATS or per == "total":
        value_col = stat
    else:
        value_col = f"{stat}_pg"
        df[value_col] = df[stat] / df["gp"]
    df = df.sort_values(value_col, ascending=bool(params.get("ascending")))
    df = df.head(int(params.get("limit", 10))).reset_index(drop=True)

    table = df[["player_name", "team_abbreviation", "gp", "mpg", value_col]].copy()
    table.insert(0, "rank", range(1, len(table) + 1))
    table["mpg"] = table["mpg"].round(1)
    table[value_col] = table[value_col].round(3 if stat in PCT_STATS else 1)

    top = table.iloc[0]
    season = df["season"].iloc[0] if "season" in df else "this season"
    label = STAT_LABELS.get(stat, stat.upper()) if per == "game" else f"total {stat}"
    answer = (f"{top['player_name']} ({top['team_abbreviation']}) leads in {label} "
              f"in {season} with {_fmt(stat, top[value_col])}")
    answer += f" (min {min_games} games)." if min_games else "."
    return {"answer": answer, "table": table,
            "data": {"leader": top["player_name"], "value": float(top[value_col])}}


def _career_totals(player):
    """Career totals row; fetched from nba_api the first time a player is asked about."""
    with _connect() as conn:
        ensure_player(conn, player)
        try:
            df = pd.read_sql("SELECT * FROM career_totals WHERE player_id = ?",
                             conn, params=[player["id"]])
        except pd.errors.DatabaseError:
            df = pd.DataFrame()
    if df.empty:
        raise QueryError(f"No career stats stored for {player['full_name']}, "
                         "and nba_api couldn't be reached.")
    return df.iloc[0]


def career_stat(params):
    """A player's regular-season career totals and per-game averages.

    params: player, stats (list, default ["pts", "reb", "ast"]) or stat.
    Players not in the database are fetched from nba_api and cached.
    """
    stats = params.get("stats") or ([params["stat"]] if params.get("stat")
                                    else ["pts", "reb", "ast"])
    for stat in stats:
        _check_stat(stat, COUNTING_STATS + list(PCT_STATS))

    player = _resolve_player(params.get("player"))
    row = _career_totals(player)
    gp = int(row["gp"])

    records, parts = [], []
    for stat in stats:
        label = STAT_LABELS.get(stat, stat.upper())
        if stat in PCT_STATS:
            made, att = PCT_STATS[stat]
            value = row[made] / row[att] if row[att] else float("nan")
            records.append({"stat": stat, "total": f"{int(row[made])}/{int(row[att])}",
                            "per_game": None, "pct": round(value, 3)})
            parts.append(f"{_fmt(stat, value)} {label}")
        else:
            per_game = row[stat] / gp if gp else float("nan")
            records.append({"stat": stat, "total": int(row[stat]),
                            "per_game": round(per_game, 1), "pct": None})
            parts.append(f"{int(row[stat]):,} {stat} ({per_game:.1f} per game)")

    answer = f"{player['full_name']} career (regular season, {gp} games): " + ", ".join(parts) + "."
    return {"answer": answer, "table": pd.DataFrame(records), "data": {"games": gp}}


# ---------------------------------------------------------------- All-NBA

ALL_NBA_VIEWS = ("age_trend", "oldest", "youngest", "season", "player", "most",
                 "career_length")


def _all_nba_frame(params):
    """All-NBA selections with each player's age on Feb 1 of that season."""
    with _connect() as conn:
        try:
            df = pd.read_sql(
                "SELECT a.season, a.start_year, a.team, a.player_name, a.player_id, "
                "b.birthdate, b.from_year, b.to_year "
                "FROM all_nba a LEFT JOIN player_bio b ON a.player_id = b.player_id", conn)
        except pd.errors.DatabaseError:
            raise QueryError("All-NBA data isn't loaded yet. Run: "
                             "venv/bin/python seed.py --all-nba")

    birth = pd.to_datetime(df["birthdate"], errors="coerce")
    feb1 = pd.to_datetime(df["start_year"].add(1).astype(str) + "-02-01")
    df["age"] = ((feb1 - birth).dt.days / 365.25).round(1)
    df["decade"] = df["start_year"] // 10 * 10

    if params.get("team"):
        df = df[df["team"] == params["team"]]
    if params.get("decade"):
        df = df[df["decade"] == int(params["decade"])]
    if params.get("season"):
        df = df[df["season"] == params["season"]]
    if params.get("player"):
        player = _resolve_player(params["player"])
        df = df[df["player_id"] == player["id"]]
        params = {**params, "_player_name": player["full_name"]}
    if df.empty:
        raise QueryError(f"No All-NBA selections{_all_nba_scope(params)}.")
    return df, params


def _all_nba_scope(params):
    parts = []
    if params.get("team"):
        parts.append(f"on the {params['team']} team")
    if params.get("season"):
        parts.append(f"in {params['season']}")
    if params.get("decade"):
        parts.append(f"in the {params['decade']}s")
    if params.get("_player_name"):
        parts.append(f"for {params['_player_name']}")
    return (" " + " ".join(parts)) if parts else ""


def all_nba(params):
    """All-NBA questions. params: view (one of ALL_NBA_VIEWS), and optional
    team ("1st"/"2nd"/"3rd"), season ("2025-26"), decade (1990), player, limit.
    "superstars" in a question is treated as All-NBA selections.
    """
    view = params.get("view", "age_trend")
    if view not in ALL_NBA_VIEWS:
        raise QueryError(f"Unknown All-NBA view '{view}'.")
    df, params = _all_nba_frame(params)
    scope = _all_nba_scope(params)
    limit = int(params.get("limit", 10))
    lead = "Using All-NBA selections as superstars: " if params.get("superstars") else ""

    if view == "age_trend":
        aged = df.dropna(subset=["age"])
        by_season = aged.groupby("season", as_index=False)["age"].mean()
        by_decade = (aged.groupby("decade")
                     .agg(selections=("age", "size"), avg_age=("age", "mean"))
                     .reset_index())
        by_decade["avg_age"] = by_decade["avg_age"].round(1)
        by_decade.insert(0, "era", by_decade.pop("decade").astype(str) + "s")

        seasons = sorted(aged["season"].unique())
        recent_seasons = seasons[-5:]
        recent = aged[aged["season"].isin(recent_seasons)]["age"].mean()
        before = aged[~aged["season"].isin(recent_seasons)]["age"].mean()
        diff = recent - before
        verdict = ("Yes, a bit older" if diff > 0.4 else
                   "No, younger" if diff < -0.4 else "About the same")
        oldest_era = by_decade.loc[by_decade["avg_age"].idxmax()]
        youngest_era = by_decade.loc[by_decade["avg_age"].idxmin()]

        answer = (f"{lead}{verdict}. Over the last 5 seasons ({recent_seasons[0]} to "
                  f"{recent_seasons[-1]}), All-NBA players{scope} averaged {recent:.1f} "
                  f"years old, vs {before:.1f} in all earlier seasons. The oldest era was "
                  f"the {oldest_era['era']} ({oldest_era['avg_age']:.1f}); the youngest "
                  f"was the {youngest_era['era']} ({youngest_era['avg_age']:.1f}).")
        smooth = by_season["age"].rolling(5, min_periods=1).mean()
        chart = {"x": by_season["season"].tolist(),
                 "y": by_season["age"].round(2).tolist(),
                 "y_smooth": smooth.round(2).tolist(),
                 "y_label": "Average age of All-NBA players",
                 "smooth_label": "5-season average"}
        return {"answer": answer, "table": by_decade,
                "data": {"recent": recent, "before": before, "chart": chart}}

    if view in ("oldest", "youngest"):
        aged = df.dropna(subset=["age"])
        oldest = view == "oldest"
        # each player once, at their oldest (or youngest) selection
        best = (aged.sort_values("age", ascending=not oldest)
                    .drop_duplicates("player_id").head(limit).reset_index(drop=True))
        table = best[["player_name", "season", "team", "age"]].copy()
        table.insert(0, "rank", range(1, len(table) + 1))
        top = table.iloc[0]
        answer = (f"{lead}The {view} All-NBA selection{scope} was {top['player_name']}, "
                  f"{top['age']:.1f} years old in {top['season']} ({top['team']} team).")
        return {"answer": answer, "table": table, "data": {}}

    if view == "season":
        season = df.sort_values("age", ascending=False)
        table = season[["player_name", "team", "age"]].reset_index(drop=True)
        aged = season.dropna(subset=["age"])
        answer = (f"The All-NBA teams{scope} averaged {aged['age'].mean():.1f} years old. "
                  f"Oldest: {aged.iloc[0]['player_name']} ({aged.iloc[0]['age']:.1f}); "
                  f"youngest: {aged.iloc[-1]['player_name']} ({aged.iloc[-1]['age']:.1f}).")
        return {"answer": answer, "table": table, "data": {}}

    if view == "player":
        counts = df["team"].value_counts()
        by_team = ", ".join(f"{counts[t]} {label}" for t, label in
                            (("1st", "first team"), ("2nd", "second"), ("3rd", "third"))
                            if t in counts)
        first, last = df["season"].min(), df["season"].max()
        name = params["_player_name"]
        answer = (f"{name} has {len(df)} All-NBA selection{'s' if len(df) != 1 else ''}"
                  f" ({by_team}), from {first} to {last}.")
        table = df.sort_values("season")[["season", "team", "age"]].reset_index(drop=True)
        return {"answer": answer, "table": table, "data": {"count": len(df)}}

    if view == "most":
        counts = (df.assign(first=df["team"].eq("1st"))
                    .groupby("player_name", as_index=False)
                    .agg(selections=("team", "size"), first_team=("first", "sum"),
                         first_season=("season", "min"), last_season=("season", "max"))
                    .sort_values(["selections", "first_team"], ascending=False)
                    .head(limit).reset_index(drop=True))
        counts.insert(0, "rank", range(1, len(counts) + 1))
        top = counts.iloc[0]
        answer = (f"{top['player_name']} has the most All-NBA selections{scope}: "
                  f"{top['selections']} ({top['first_team']} first team).")
        return {"answer": answer, "table": counts, "data": {}}

    # career_length: retired players only, grouped by the decade of their first selection
    start_now = int(CURRENT_SEASON[:4])
    first = (df.dropna(subset=["from_year", "to_year"])
               .sort_values("start_year").drop_duplicates("player_id"))
    retired = first[first["to_year"] < start_now - 1].copy()
    retired["seasons"] = retired["to_year"] - retired["from_year"] + 1
    by_decade = (retired.groupby("decade")
                 .agg(players=("seasons", "size"), avg_seasons=("seasons", "mean"))
                 .reset_index())
    by_decade["avg_seasons"] = by_decade["avg_seasons"].round(1)
    by_decade.insert(0, "first_all_nba", by_decade.pop("decade").astype(str) + "s")
    longest = by_decade.loc[by_decade["avg_seasons"].idxmax()]
    shortest = by_decade.loc[by_decade["avg_seasons"].idxmin()]
    answer = (f"{lead}Retired All-NBA players{scope} played {retired['seasons'].mean():.1f} "
              f"seasons on average. Careers were longest for players first named in the "
              f"{longest['first_all_nba']} ({longest['avg_seasons']:.1f}) and shortest for the "
              f"{shortest['first_all_nba']} ({shortest['avg_seasons']:.1f}). Players still "
              f"active are left out, so recent decades only count finished careers.")
    chart = {"x": by_decade["first_all_nba"].tolist(),
             "y": by_decade["avg_seasons"].tolist(),
             "y_label": "Average career length (seasons)", "type": "bar"}
    return {"answer": answer, "table": by_decade, "data": {"chart": chart}}


# ---------------------------------------------------------------- all-time leaders

STAT_WORDS = {"pts": "points", "reb": "rebounds", "ast": "assists", "stl": "steals",
              "blk": "blocks", "tov": "turnovers", "fgm": "field goals", "fga": "field goal attempts",
              "fg3m": "3-pointers", "fg3a": "3-point attempts", "ftm": "free throws",
              "fta": "free throw attempts", "oreb": "offensive rebounds",
              "dreb": "defensive rebounds", "pf": "fouls", "gp": "games",
              "fg_pct": "field goal percentage", "fg3_pct": "3-point percentage",
              "ft_pct": "free throw percentage"}


def _years_since(born):
    return (pd.Timestamp.today() - pd.Timestamp(born)).days / 365.25 if born else None


def _recent_pace(totals, stat):
    """Average per season over the last 3 complete regular seasons."""
    if totals is None or totals.empty or stat not in totals:
        return None
    done = totals[totals["season"] != CURRENT_SEASON]
    last = done.tail(3)
    return float(last[stat].mean()) if len(last) else None


def all_time(params):
    """All-time career leaders (regular season, top 25 from nba_api).

    params: stat (default "pts"), player (where does he rank), chase (True for
    "will anyone beat X's record?": projects active players' paths to #1), limit.
    """
    stat = _check_stat(params.get("stat", "pts"), list(STAT_WORDS))
    words = STAT_WORDS[stat]
    with _connect() as conn:
        ensure_all_time_leaders(conn)
        try:
            board = pd.read_sql("SELECT * FROM all_time_leaders WHERE stat = ? ORDER BY rank",
                                conn, params=[stat])
        except pd.errors.DatabaseError:
            board = pd.DataFrame()
    if board.empty:
        raise QueryError("All-time leaders aren't loaded, and nba_api couldn't be reached.")

    is_pct = stat in PCT_STATS
    fmt = (lambda v: f"{v:.1%}") if is_pct else (lambda v: f"{int(v):,}")
    holder = board.iloc[0]
    table = board[["rank", "player_name", "value", "active"]].copy()
    table["active"] = table["active"].map({1: "yes", 0: "", True: "yes", False: ""})
    table = table.rename(columns={"value": stat})

    if params.get("player"):
        player = _resolve_player(params["player"])
        row = board[board["player_id"] == player["id"]]
        if row.empty:
            answer = (f"{player['full_name']} isn't in the all-time top 25 in {words}; "
                      f"#25 is {board.iloc[-1]['player_name']} with {fmt(board.iloc[-1]['value'])}.")
        else:
            r = row.iloc[0]
            answer = (f"{player['full_name']} ranks #{int(r['rank'])} all-time in {words} "
                      f"with {fmt(r['value'])}.")
            if int(r["rank"]) > 1:
                answer += f" #1 is {holder['player_name']} with {fmt(holder['value'])}."
        return {"answer": answer, "table": table.head(int(params.get("limit", 25))), "data": {}}

    if not params.get("chase") or is_pct:
        answer = (f"{holder['player_name']} is the all-time leader in {words} "
                  f"with {fmt(holder['value'])}.")
        return {"answer": answer, "table": table.head(int(params.get("limit", 10))), "data": {}}

    # "Will anyone beat the record?": project active challengers
    record = float(holder["value"])
    holder_pace = 0.0
    if holder["active"]:
        holder_pace = _recent_pace(season_totals(int(holder["player_id"])), stat) or 0.0
    challengers = board[(board["active"].astype(bool)) & (board["rank"] > 1)].head(6)

    rows = []
    with _connect() as conn:
        for _, c in challengers.iterrows():
            pace = _recent_pace(season_totals(int(c["player_id"])), stat)
            age = _years_since(birthdate(conn, int(c["player_id"])))
            gap = record - c["value"]
            # seasons to pass today's mark; the holder may keep adding to it
            seasons = gap / pace if pace else None
            rows.append({"rank": int(c["rank"]), "player_name": c["player_name"],
                         stat: int(c["value"]), "behind": int(gap),
                         "per_season": round(pace) if pace else None,
                         "age": round(age, 1) if age else None,
                         "seasons_needed": round(seasons, 1) if seasons else None,
                         "age_at_record": round(age + seasons, 1) if seasons and age else None})
    chase = pd.DataFrame(rows)
    if chase.empty:
        raise QueryError(f"No active players in the all-time top 25 in {words}.")

    able = chase.dropna(subset=["seasons_needed"]).sort_values("seasons_needed")
    moving = (f" and is still adding about {holder_pace:,.0f} a season, so the real target "
              f"will be higher" if holder["active"] and holder_pace else "")
    lead = (f"{holder['player_name']} holds the record with {fmt(record)} {words}{moving}.")
    if able.empty:
        answer = (f"Unlikely. {lead} No active player in the all-time top 25 has a recent "
                  f"pace to project from.")
    else:
        best = able.iloc[0]
        years, age_at = best["seasons_needed"], best["age_at_record"]
        verdict = ("Likely" if years <= 3 and (age_at or 0) <= 38 else
                   "Possibly" if years <= 6 and (age_at or 0) <= 39 else "Unlikely")
        answer = (f"{verdict}. {lead} The closest challenger on pace is {best['player_name']} "
                  f"({best[stat]:,}, {best['behind']:,} behind), averaging about "
                  f"{best['per_season']:,} {words} a season over his last 3 seasons. "
                  f"At that pace he'd pass today's mark in about {years:.1f} seasons")
        answer += f" and would be about {age_at:.0f}." if age_at else "."
    answer += " Only players already in the all-time top 25 are considered."
    return {"answer": answer, "table": chase, "data": {"record": record}}


HANDLERS = {
    "all_time": all_time,
    "all_nba": all_nba,
    "best_season": best_season,
    "best_games": best_games,
    "game_count": game_count,
    "player_averages": player_averages,
    "league_leaders": league_leaders,
    "career_stat": career_stat,
    "compare_players": compare_players,
    "record": record,
    "streak": streak,
    "recent_games": recent_games,
}


def follow_query(category, query):
    """Run the handler for an intent category on a dict of structured params."""
    handler = HANDLERS.get(category)
    if handler is None:
        _log_failed(category, query)
        return {"category": category, "table": None, "data": {},
                "answer": "Sorry, we're not capable of answering that."}
    try:
        result = handler(query or {})
    except QueryError as e:
        _log_failed(category, query)
        return {"category": category, "answer": str(e), "table": None, "data": {}}
    return {"category": category, **result}
