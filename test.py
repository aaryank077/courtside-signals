from nba_api.stats.endpoints import playercareerstats
from nba_api.stats.static import players
import pandas as pd
import numpy as np
import time

# Read and clean data
df = pd.read_csv("all-nba.csv")

df = df[df["Lg"] == "NBA"]

df.columns = [
    "Season",
    "Lg",
    "Team",
    "Voting",
    "Player1",
    "Player2",
    "Player3",
    "Player4",
    "Player5",
]

# Remove position suffixes (G, F, C, W)
for col in ["Player1", "Player2", "Player3", "Player4", "Player5"]:
    df[col] = (
        df[col]
        .str.replace(r"\s[CFGW]$", "", regex=True)
        .str.strip()
    )

# Create player lookup
player_lookup = {
    p["full_name"]: p["id"]
    for p in players.get_players()
}

career_cache = {}

def career_length(player_name):
    """Returns number of NBA seasons played."""

    if player_name in career_cache:
        return career_cache[player_name]

    player_id = player_lookup.get(player_name)

    if player_id is None:
        career_cache[player_name] = np.nan
        return np.nan

    try:
        career = playercareerstats.PlayerCareerStats(player_id=player_id)
        seasons = career.get_data_frames()[0]

        length = seasons["SEASON_ID"].nunique()

        career_cache[player_name] = length

        # avoid hammering NBA API
        time.sleep(0.5)

        return length

    except:
        career_cache[player_name] = np.nan
        return np.nan

# Compute average career length
player_cols = ["Player1", "Player2", "Player3", "Player4", "Player5"]

for col in player_cols:
    df[col + "_Career"] = df[col].apply(career_length)

career_cols = [col + "_Career" for col in player_cols]

df["Average Career Length"] = df[career_cols].mean(axis=1)

# Average by season
season_avg = (
    df.groupby("Season")["Average Career Length"]
      .mean()
      .reset_index()
)

print(season_avg)

"""
player = players.find_players_by_full_name("LeBron James")
print(player)
print(player[0]['id'])

test = playercareerstats.PlayerCareerStats('2544')
print(test.get_data_frames()[0])

test = playercareerstats.PlayerCareerStats(2544)
print(test.get_data_frames()[0])

test = playercareerstats.PlayerCareerStats(player[0]['id'])
print(test.get_data_frames()[0])

test = playercareerstats.PlayerCareerStats(int(player[0]['id']))
print(test.get_data_frames()[0])


stats = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26')
df = stats.get_data_frames()[0]
df['AST_GP'] = df['AST'] / df['GP']
df['MIN_GP'] = df['MIN'] / df['GP']
df['PTS_GP'] = df['PTS'] / df['GP']
df['REB_GP'] = df['REB'] / df['GP']

leaders = df[(df['MIN_GP'] >= 30)].sort_values('AST_GP', ascending=False)
print(leaders[['PLAYER_NAME', 'GP', 'W', 'MIN_GP', 'PTS_GP', 'REB_GP', 
               'AST_GP']].head(10))
"""