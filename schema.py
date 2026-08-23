import sqlite3

CREATE_GAMES_TABLE = """
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT,
    player_id INTEGER,
    player_name TEXT,
    season_id TEXT,
    game_date TEXT,
    matchup TEXT,
    wl TEXT,
    min REAL,
    fgm INTEGER,
    fga INTEGER,
    fg_pct REAL,
    fg3m INTEGER,
    fg3a INTEGER,
    fg3_pct REAL,
    ftm INTEGER,
    fta INTEGER,
    ft_pct REAL,
    oreb INTEGER,
    dreb INTEGER,
    reb INTEGER,
    ast INTEGER,
    stl INTEGER,
    blk INTEGER,
    tov INTEGER,
    pf INTEGER,
    pts INTEGER,
    plus_minus REAL,
    video_available INTEGER
);
"""

if __name__ == "__main__":
    conn = sqlite3.connect('courtside.db')
    cursor = conn.cursor()
    cursor.execute(CREATE_GAMES_TABLE)
    conn.commit()
    conn.close()
    print("Database created successfully")