"""Fetch data from nba_api into courtside.db when a question needs it.

- ensure_player(conn, player): the first time a player is asked about, fetch
  every regular-season game of their career plus career totals. After that,
  refresh the current season and career totals at most once a day.
- ensure_league_stats(conn): refresh this season's league_stats table at most
  once a day.

What was fetched when is kept in the refresh_log table. Set the environment
variable COURTSIDE_OFFLINE=1 to never call nba_api (tests use this).
"""

import os
import sys
import time
from datetime import date

import pandas as pd
from nba_api.stats.endpoints import leaguedashplayerstats, playercareerstats, playergamelog

from engine.vocab import CURRENT_SEASON

CREATE_REFRESH_LOG = """
CREATE TABLE IF NOT EXISTS refresh_log (
    key TEXT PRIMARY KEY,
    refreshed_on TEXT
)
"""


def offline():
    return os.environ.get("COURTSIDE_OFFLINE") == "1"


def _say(message):
    # progress goes to stderr so it doesn't mix with answers
    print(message, file=sys.stderr, flush=True)


def with_retries(fetch, label, attempts=3):
    for attempt in range(attempts):
        try:
            return fetch()
        except Exception as e:
            _say(f"  attempt {attempt + 1} failed for {label}: {e}")
            time.sleep(2)
    return None


# ---------------------------------------------------------------- refresh log

def _last_refresh(conn, key):
    conn.execute(CREATE_REFRESH_LOG)
    row = conn.execute("SELECT refreshed_on FROM refresh_log WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _mark_refreshed(conn, key):
    conn.execute("INSERT OR REPLACE INTO refresh_log VALUES (?, ?)",
                 (key, date.today().isoformat()))
    conn.commit()


# ---------------------------------------------------------------- fetching

def fetch_career(player_id):
    """(regular-season labels like '2018-19', career totals DataFrame) or (None, None)."""
    frames = with_retries(
        lambda: playercareerstats.PlayerCareerStats(player_id=player_id, timeout=15)
        .get_data_frames(), "career stats")
    if frames is None:
        return None, None
    seasons = sorted(frames[0]["SEASON_ID"].unique().tolist())
    return seasons, frames[1]


def fetch_game_logs(player_id, seasons):
    frames = []
    for season in seasons:
        df = with_retries(
            lambda: playergamelog.PlayerGameLog(player_id=player_id, season=season,
                                                timeout=15).get_data_frames()[0],
            season)
        if df is not None and not df.empty:
            frames.append(df)
        time.sleep(0.6)  # be gentle with stats.nba.com
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).drop_duplicates(subset="Game_ID")


def store_games(conn, df, player_name):
    """Append games not already stored. Returns how many were added."""
    if df.empty:
        return 0
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df["player_name"] = player_name
    try:
        have = {r[0] for r in conn.execute(
            "SELECT game_id FROM games WHERE player_id = ?", (int(df["player_id"].iloc[0]),))}
    except Exception:
        have = set()  # games table doesn't exist yet; to_sql creates it
    df = df[~df["game_id"].isin(have)]
    if not df.empty:
        df.to_sql("games", conn, if_exists="append", index=False)
    return len(df)


def store_career_totals(conn, totals, player_name):
    if totals is None or totals.empty:
        return
    totals = totals.copy()
    totals.insert(1, "PLAYER_NAME", player_name)
    totals.columns = [c.lower() for c in totals.columns]
    try:
        conn.execute("DELETE FROM career_totals WHERE player_id = ?",
                     (int(totals["player_id"].iloc[0]),))
    except Exception:
        pass  # table doesn't exist yet; to_sql creates it
    totals.to_sql("career_totals", conn, if_exists="append", index=False)


# ---------------------------------------------------------------- public

def ensure_player(conn, player):
    """Make sure a player's games and career totals are stored and fresh."""
    if offline():
        return
    pid, name = player["id"], player["full_name"]
    today = date.today().isoformat()
    full_key, daily_key = f"full:{pid}", f"daily:{pid}"

    if _last_refresh(conn, full_key) is None:
        seasons, totals = fetch_career(pid)
        if seasons is None:
            _say(f"Couldn't reach nba_api for {name}; using what's stored.")
            return
        _say(f"Fetching {name}'s games ({len(seasons)} seasons, first time only)...")
        added = store_games(conn, fetch_game_logs(pid, seasons), name)
        store_career_totals(conn, totals, name)
        _say(f"  saved {added} games")
        _mark_refreshed(conn, full_key)
        _mark_refreshed(conn, daily_key)
        return

    if _last_refresh(conn, daily_key) != today and player.get("is_active", True):
        seasons, totals = fetch_career(pid)
        if seasons is None:
            return  # offline or rate limited: answer from stored data
        if CURRENT_SEASON in seasons:
            store_games(conn, fetch_game_logs(pid, [CURRENT_SEASON]), name)
        store_career_totals(conn, totals, name)
        _mark_refreshed(conn, daily_key)


def ensure_league_stats(conn):
    """Refresh this season's league_stats at most once a day."""
    if offline() or _last_refresh(conn, "league") == date.today().isoformat():
        return
    df = with_retries(
        lambda: leaguedashplayerstats.LeagueDashPlayerStats(
            season=CURRENT_SEASON, timeout=30).get_data_frames()[0],
        "league stats")
    if df is None:
        _say("Couldn't refresh league stats; using what's stored.")
        return
    df.columns = [c.lower() for c in df.columns]
    df["season"] = CURRENT_SEASON
    df.to_sql("league_stats", conn, if_exists="replace", index=False)
    _mark_refreshed(conn, "league")


# ---------------------------------------------------------------- all-time leaders

def ensure_all_time_leaders(conn):
    """All-time top 25 in each stat (regular season), refreshed once a day.
    Stored long-form: all_time_leaders(stat, rank, player_id, player_name, value, active)."""
    if offline() or _last_refresh(conn, "alltime") == date.today().isoformat():
        return
    from nba_api.stats.endpoints import alltimeleadersgrids
    frames = with_retries(
        lambda: alltimeleadersgrids.AllTimeLeadersGrids(topx=25, timeout=20).get_data_frames(),
        "all-time leaders")
    if frames is None:
        _say("Couldn't refresh all-time leaders; using what's stored.")
        return
    rows = []
    for f in frames:
        stat = f.columns[2]
        for _, r in f.iterrows():
            rows.append({"stat": stat.lower(), "rank": int(r[f"{stat}_RANK"]),
                         "player_id": int(r["PLAYER_ID"]), "player_name": r["PLAYER_NAME"],
                         "value": float(r[stat]), "active": r["IS_ACTIVE_FLAG"] == "Y"})
    pd.DataFrame(rows).to_sql("all_time_leaders", conn, if_exists="replace", index=False)
    _mark_refreshed(conn, "alltime")


def season_totals(player_id):
    """One row per regular season (traded players' combined 'TOT' row kept),
    columns lowercased, plus 'season'. None if nba_api can't be reached."""
    if offline():
        return None
    frames = with_retries(
        lambda: playercareerstats.PlayerCareerStats(player_id=player_id, timeout=15)
        .get_data_frames(), "career stats")
    if frames is None:
        return None
    df = frames[0].copy()
    df.columns = [c.lower() for c in df.columns]
    df["season"] = df["season_id"]
    # a traded player has a row per team plus a TOT row; keep the biggest (TOT)
    return df.sort_values("gp").drop_duplicates("season", keep="last").sort_values("season")


def birthdate(conn, player_id):
    """Birthdate 'YYYY-MM-DD' from player_bio, fetching and storing it if needed."""
    conn.execute("""CREATE TABLE IF NOT EXISTS player_bio (
        player_id INTEGER PRIMARY KEY, player_name TEXT, birthdate TEXT,
        from_year INTEGER, to_year INTEGER, season_exp INTEGER)""")
    row = conn.execute("SELECT birthdate FROM player_bio WHERE player_id = ?",
                       (player_id,)).fetchone()
    if row and row[0]:
        return row[0]
    if offline():
        return None
    from nba_api.stats.endpoints import commonplayerinfo
    info = with_retries(lambda: commonplayerinfo.CommonPlayerInfo(
        player_id=player_id, timeout=15).get_data_frames()[0], "player info")
    if info is None or info.empty:
        return None
    r = info.iloc[0]
    born = str(r["BIRTHDATE"])[:10]
    conn.execute("INSERT OR REPLACE INTO player_bio VALUES (?, ?, ?, ?, ?, ?)",
                 (player_id, r["DISPLAY_FIRST_LAST"], born,
                  int(r["FROM_YEAR"]) if pd.notna(r["FROM_YEAR"]) else None,
                  int(r["TO_YEAR"]) if pd.notna(r["TO_YEAR"]) else None,
                  int(r["SEASON_EXP"]) if pd.notna(r["SEASON_EXP"]) else None))
    conn.commit()
    return born
