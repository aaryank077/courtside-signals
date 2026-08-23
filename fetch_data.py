import pandas as pd
import numpy as np
from pandas import DataFrame
from nba_api.stats.static import players
from nba_api.stats.endpoints import playercareerstats, playergamelog, leaguedashplayerstats
import time
import sqlite3

"""
This responds to 5 templatized queries primarily using nba_api 
(https://github.com/swar/nba_api) as well as other data resources available like
Basketball Reference (https://www.basketball-reference.com/). The queries are as
follow:

1. Steph Curry's best 3-point shooting season with at least 400 attempts
2. How many 30-point games does Luka have against the Lakers?
3. What are Jokić's rebound numbers against the Celtics?
4. Who leads the league in assists per game this season among players averaging 
30+ minutes?
5. What has been the average career length of All-NBA players over time?
"""

def convert_season_id(season):
    season = season[1:]
    end = str(int(season[2:]) + 1)
    return f"{season}-{end}"

### QUERY 1

player = players.find_players_by_full_name("Stephen Curry")

conn = sqlite3.connect('courtside.db')
df = pd.read_sql("SELECT * FROM games WHERE player_name = 'Stephen Curry'", conn)

print("Query 1: Steph Curry's best 3-point shooting season with at least 400 attempts")
print((df[(df['fg3a'] > 400)].sort_values('fg3_pct', ascending=False)).iloc[0])

### QUERY 2

player = players.find_players_by_full_name("Luka Dončić")


seasons = ['2018-19', '2019-20', '2020-21', '2021-22', '2022-23', '2023-24', 
            '2024-25', '2025-26']

conn = sqlite3.connect('courtside.db')
df = pd.read_sql("SELECT * FROM games WHERE player_name = 'Luka Dončić'", conn)

print("How many 30-point games does Luka have against the Lakers?")
print(len(df[(df['pts'] >= 30) & df['matchup'].str.contains('lal') & 
                ~df['matchup'].str.startswith('lal')]))

### QUERY 3

player = players.find_players_by_full_name("Nikola Jokić")
seasons = ['2015-16', '2016-17', '2017-18', '2018-19', '2019-20', '2020-21', 
           '2021-22', '2022-23', '2023-24', '2024-25', '2025-26']

conn = sqlite3.connect('courtside.db')
df = pd.read_sql("SELECT * FROM games WHERE player_name = 'Nikola Jokić'", conn)

print("What are Nikola Jokić's rebound numbers against the Celtics?")
matchups = df[df['matchup'].str.contains('bos')][['season_id', 'game_date', 
                                                  'reb']]
print(matchups)
print("Average: ", round(matchups['reb'].mean(), 1))

### QUERY 4
stats = leaguedashplayerstats.LeagueDashPlayerStats(season='2025-26')
df = stats.get_data_frames()[0]
df['AST_GP'] = df['AST'] / df['GP']
df['MIN_GP'] = df['MIN'] / df['GP']
df['PTS_GP'] = df['PTS'] / df['GP']
df['REB_GP'] = df['REB'] / df['GP']

print("Who leads the league in assists per game this season among players averaging 30+ minutes?")
leaders = df[(df['MIN_GP'] >= 30)].sort_values('AST_GP', ascending=False)
print(leaders[['PLAYER_NAME', 'GP', 'MIN_GP', 'PTS_GP', 'REB_GP', 
               'AST_GP']].head(10))

### QUERY 5

# Read and clean data imported from https://www.basketball-reference.com/awards/all_league.html
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

print("What has been the average career length of All-NBA players over time?")
print(season_avg)