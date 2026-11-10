import sqlite3
import pandas as pd
import time
from nba_api.stats.static import players
from nba_api.stats.endpoints import playergamelog, leaguedashplayerstats, playercareerstats

from schema import DB_PATH, CREATE_GAMES_TABLE
from engine.vocab import CURRENT_SEASON

# Players to seed
PLAYERS = ["Stephen Curry", "Luka Dončić", "Nikola Jokić"]
SEASONS = ['2018-19', '2019-20', '2020-21', '2021-22', '2022-23', '2023-24',
           '2024-25', '2025-26']


def get_player(name):
    player = players.find_players_by_full_name(name)
    return player[0]


def with_retries(fetch, label, attempts=3):
    for attempt in range(attempts):
        try:
            return fetch()
        except Exception as e:
            print(f"  Attempt {attempt + 1} failed for {label}: {e}")
            time.sleep(2)
    return None


def fetch_game_logs(player_id, seasons):
    all_games = []
    for season in seasons:
        log_df = with_retries(
            lambda: playergamelog.PlayerGameLog(
                player_id=player_id, season=season, timeout=10
            ).get_data_frames()[0],
            season,
        )
        if log_df is not None:
            all_games.append(log_df)
        time.sleep(1)
    if not all_games:
        return pd.DataFrame()
    return pd.concat(all_games).drop_duplicates(subset='Game_ID')


def fetch_career_totals(player_id):
    """One row of regular-season career totals for a player."""
    return with_retries(
        lambda: playercareerstats.PlayerCareerStats(
            player_id=player_id, timeout=10
        ).get_data_frames()[1],
        "career totals",
    )


def leaguestats():
    return leaguedashplayerstats.LeagueDashPlayerStats(
        season=CURRENT_SEASON, timeout=30
    ).get_data_frames()[0]


def seed():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(CREATE_GAMES_TABLE)

    # fetch what's already in the database
    existing = pd.read_sql("SELECT game_id, player_id FROM games", conn)
    existing_keys = set(zip(existing['game_id'], existing['player_id']))

    career_frames = []

    for player_name in PLAYERS:
        print(f"Seeding {player_name}...")
        player_id = get_player(player_name)['id']

        # PlayerGameLog doesn't return a name, so fill it in for older rows too
        conn.execute(
            "UPDATE games SET player_name = ? WHERE player_id = ? "
            "AND (player_name IS NULL OR player_name = '')",
            (player_name, player_id),
        )

        df = fetch_game_logs(player_id, SEASONS)
        if df.empty:
            print(f"  No game logs for {player_name}")
        else:
            df.columns = [col.lower() for col in df.columns]
            df['player_name'] = player_name

            # filter out already-inserted games
            keys = list(zip(df['game_id'], df['player_id']))
            df = df[[k not in existing_keys for k in keys]]

            if df.empty:
                print("  No new games")
            else:
                df.to_sql('games', conn, if_exists='append', index=False)
                print(f"  Inserted {len(df)} games")

        career = fetch_career_totals(player_id)
        if career is not None and not career.empty:
            career = career.copy()
            career.insert(1, 'PLAYER_NAME', player_name)
            career_frames.append(career)
        time.sleep(1)

    conn.commit()

    if career_frames:
        print("Seeding career totals...")
        df = pd.concat(career_frames)
        df.columns = [col.lower() for col in df.columns]
        df.to_sql('career_totals', conn, if_exists='replace', index=False)
        print(f"  Inserted {len(df)} rows")

    print("Seeding league stats...")
    df = with_retries(leaguestats, "league stats")
    if df is not None:
        df.columns = [col.lower() for col in df.columns]
        df['season'] = CURRENT_SEASON
        # this is a snapshot of the season so far, so replace it on each run
        df.to_sql('league_stats', conn, if_exists='replace', index=False)
        print(f"  Inserted {len(df)} rows")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    seed()
