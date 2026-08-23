import sqlite3
import pandas as pd
import time
from nba_api.stats.static import players
from nba_api.stats.endpoints import playergamelog, leaguedashplayerstats

# Players to seed
PLAYERS = ["Stephen Curry", "Luka Dončić", "Nikola Jokić"]
SEASONS = ['2018-19', '2019-20', '2020-21', '2021-22', '2022-23', '2023-24', 
           '2024-25', '2025-26']

def get_player_id(name):
    player = players.find_players_by_full_name(name)
    return player[0]['id']

def fetch_game_logs(player_id, seasons):
    all_games = []
    game_ids = set()
    for season in seasons:
        for attempt in range(3):
            try:
                log = playergamelog.PlayerGameLog(player_id=player_id, season=season, timeout=10)
                log_df = log.get_data_frames()[0]
                log_df = log_df.drop_duplicates(subset='Game_ID')
                log_df = log_df[~log_df['Game_ID'].isin(game_ids)]
                all_games.append(log_df)
                game_ids.update(log_df['Game_ID'])
                time.sleep(1)
                break
            except Exception as e:
                print(f"  Attempt {attempt + 1} failed for {season}: {e}")
                time.sleep(2)
    return pd.concat(all_games) if all_games else pd.DataFrame()

def leaguestats():
    stats = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26')
    df = stats.get_data_frames()[0]

    return df

def seed():
    conn = sqlite3.connect('courtside.db')
    
    # fetch what's already in the database
    existing = pd.read_sql("SELECT game_id FROM games", conn)
    existing_ids = set(existing['game_id'].tolist())
    
    for player_name in PLAYERS:
        print(f"Seeding {player_name}...")
        player_id = get_player_id(player_name)
        df = fetch_game_logs(player_id, SEASONS)
        
        if df.empty:
            print(f"  No data for {player_name}, skipping")
            continue

        df.columns = [col.lower() for col in df.columns]
        
        # filter out already-inserted games
        df = df.drop_duplicates(subset='Game_ID')
        df = df[~df['game_id'].isin(existing_ids)]
        
        if df.empty:
            print(f"  No new games for {player_name}, skipping")
            continue
            
        df.to_sql('games', conn, if_exists='append', index=False)
        print(f"  Inserted {len(df)} rows")
        time.sleep(1)

    df = leaguestats()
    df.to_sql('player_id', conn, f_exists='append', index=False)
    
    conn.close()
    print("Done.")

if __name__ == "__main__":
    seed()