from dataclasses import dataclass

@dataclass
class GameLog:
    player_id: str
    player_name: str
    date: str
    opponent: str
    season: str
    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    fg_pct: float
    fg3_pct: float
    ft_pct: float
    minutes: float
    game_score: float
    team: str
    home_away: str

