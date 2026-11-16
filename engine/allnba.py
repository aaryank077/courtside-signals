"""Load All-NBA selections (all-nba.csv, from Basketball Reference) and player
bios (birthdate, career span, from nba_api) into courtside.db.

    venv/bin/python seed.py --all-nba

Tables:
    all_nba(season, start_year, team, player_name, player_id, league)
    player_bio(player_id, player_name, birthdate, from_year, to_year, season_exp)

Loading bios is one nba_api call per player (~260 players, ~4 minutes) and
only fetches players not already stored, so re-running is quick.
"""

import re
import sqlite3
import time
import unicodedata
from pathlib import Path

import pandas as pd
from nba_api.stats.endpoints import commonplayerinfo
from nba_api.stats.static import players

from engine.fetch import with_retries

CSV_PATH = Path(__file__).resolve().parent.parent / "all-nba.csv"

# Basketball Reference name -> nba_api full name, where they differ
ALIASES = {
    "fat lever": "Lafayette Lever",
    "tiny archibald": "Nate Archibald",
    "jimmy butler": "Jimmy Butler III",
    "world b free": "World Free",
    "jo jo white": "Jojo White",
    "frankie brian": "Frank Brian",
    "bones mckinney": "Horace McKinney",
    "chick halbert": "Chuck Halbert",
    "fred scolari": "Freddie Scolari",
    "john logan": "Johnny Logan",
}


def _norm(name):
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[.']", "", name)).strip()


def _player_lookup():
    """Normalized name -> every nba_api player with that name (fathers and
    sons, namesakes like the two Larry Johnsons)."""
    lookup = {}
    for p in players.get_players():
        lookup.setdefault(_norm(p["full_name"]), []).append(p)
    return lookup


def read_selections():
    """One row per (season, team, player) from all-nba.csv; NBA and BAA only.
    Rows whose name matches several players list them all in `candidates`;
    load() keeps the one whose career covers that season."""
    raw = pd.read_csv(CSV_PATH)
    raw.columns = ["season", "league", "team", "voting", "p1", "p2", "p3", "p4", "p5"]
    raw = raw[raw["league"].isin(["NBA", "BAA"])]

    lookup = _player_lookup()
    rows, unmatched = [], set()
    for _, r in raw.iterrows():
        for cell in r[["p1", "p2", "p3", "p4", "p5"]].dropna():
            # a tie puts two players in one cell: "Bob Davies  Dolph Schayes (T)"
            for name in re.split(r"\s{2,}", str(cell).replace("(T)", "")):
                name = re.sub(r"\s[CFGW]$", "", name.strip()).strip()
                if not name:
                    continue
                key = _norm(name)
                matches = lookup.get(_norm(ALIASES.get(key, name)), [])
                if not matches:
                    unmatched.add(name)
                rows.append({
                    "season": r["season"],
                    "start_year": int(r["season"][:4]),
                    "team": r["team"],
                    "player_name": matches[0]["full_name"] if matches else name,
                    "player_id": matches[0]["id"] if matches else None,
                    "candidates": [p["id"] for p in matches],
                    "league": r["league"],
                })
    return pd.DataFrame(rows), sorted(unmatched)


def _fetch_bios(conn, ids):
    conn.execute("""CREATE TABLE IF NOT EXISTS player_bio (
        player_id INTEGER PRIMARY KEY, player_name TEXT, birthdate TEXT,
        from_year INTEGER, to_year INTEGER, season_exp INTEGER)""")
    have = {r[0] for r in conn.execute("SELECT player_id FROM player_bio")}
    todo = [i for i in ids if i not in have]
    print(f"  fetching bios for {len(todo)} players...")

    for n, pid in enumerate(todo, 1):
        info = with_retries(
            lambda: commonplayerinfo.CommonPlayerInfo(player_id=pid, timeout=15)
            .get_data_frames()[0], f"player {pid}")
        if info is not None and not info.empty:
            row = info.iloc[0]
            conn.execute(
                "INSERT OR REPLACE INTO player_bio VALUES (?, ?, ?, ?, ?, ?)",
                (pid, row["DISPLAY_FIRST_LAST"], str(row["BIRTHDATE"])[:10] or None,
                 int(row["FROM_YEAR"]) if pd.notna(row["FROM_YEAR"]) else None,
                 int(row["TO_YEAR"]) if pd.notna(row["TO_YEAR"]) else None,
                 int(row["SEASON_EXP"]) if pd.notna(row["SEASON_EXP"]) else None))
            conn.commit()
        if n % 25 == 0:
            print(f"    {n}/{len(todo)}")
        time.sleep(0.6)


def load(conn):
    df, unmatched = read_selections()
    ids = sorted({int(i) for c in df["candidates"] for i in c})
    _fetch_bios(conn, ids)

    # same name, several players: keep whoever was playing that season
    spans = {r[0]: (r[1], r[2]) for r in
             conn.execute("SELECT player_id, from_year, to_year FROM player_bio")}
    names = dict(conn.execute("SELECT player_id, player_name FROM player_bio").fetchall())
    for i, row in df[df["candidates"].map(len) > 1].iterrows():
        for pid in row["candidates"]:
            lo, hi = spans.get(pid, (None, None))
            if lo is not None and lo <= row["start_year"] <= hi:
                df.at[i, "player_id"] = pid
                df.at[i, "player_name"] = names.get(pid, row["player_name"])
                break

    df = df.drop(columns="candidates")
    df.to_sql("all_nba", conn, if_exists="replace", index=False)
    print(f"  {len(df)} All-NBA selections, {df['player_id'].nunique()} players")
    if unmatched:
        print(f"  no nba_api match (ages unknown): {', '.join(unmatched)}")


if __name__ == "__main__":
    from schema import DB_PATH
    load(sqlite3.connect(DB_PATH))
