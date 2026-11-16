"""Pre-load data into courtside.db.

    venv/bin/python seed.py             # pre-load a few players and league stats
    venv/bin/python seed.py --all-nba   # load All-NBA selections and player ages (~4 min once)

Pre-loading players is optional: any player a question asks about is fetched
the first time (see engine/fetch.py). The All-NBA load is needed for
superstar / All-NBA questions. Safe to re-run; it only adds what's missing.
"""

import sqlite3
import sys

from nba_api.stats.static import players

from engine import allnba
from engine.fetch import ensure_league_stats, ensure_player
from schema import CREATE_GAMES_TABLE, DB_PATH

PLAYERS = ["Stephen Curry", "Luka Dončić", "Nikola Jokić"]


def seed():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_GAMES_TABLE)
    for name in PLAYERS:
        print(f"Seeding {name}...")
        ensure_player(conn, players.find_players_by_full_name(name)[0])
    print("Seeding league stats...")
    ensure_league_stats(conn)
    conn.close()
    print("Done.")


def seed_all_nba():
    conn = sqlite3.connect(DB_PATH)
    print("Loading All-NBA selections and player bios...")
    allnba.load(conn)
    conn.close()
    print("Done.")


if __name__ == "__main__":
    if "--all-nba" in sys.argv:
        seed_all_nba()
    else:
        seed()
