"""Pre-load players into courtside.db.

You don't have to run this: any player a question asks about is fetched the
first time (see engine/fetch.py). Seeding just does that work up front so the
first questions are fast. Safe to re-run; it only adds what's missing.
"""

import sqlite3

from nba_api.stats.static import players

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


if __name__ == "__main__":
    seed()
