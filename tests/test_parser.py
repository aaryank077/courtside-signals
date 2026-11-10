"""Parser tests. Run from the repo root with either:

    venv/bin/python tests/test_parser.py
    venv/bin/python -m pytest tests        (if pytest is installed)

PARSE_CASES only need nba_api's built-in player list (no network). The
ANSWER_CASES also read courtside.db and are skipped if it hasn't been seeded.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.parser import ask, parse  # noqa: E402

PARSE_CASES = [
    # the five original questions from fetch_data.py
    ("Steph Curry's best 3-point shooting season with at least 400 attempts",
     "best_season", {"player": "Stephen Curry", "stat": "fg3_pct", "min": {"fg3a": 400}}),
    ("How many 30-point games does Luka have against the Lakers?",
     "game_count", {"player": "Luka Dončić", "conditions": {"pts": 30}, "opponent": "LAL"}),
    ("What are Jokić's rebound numbers against the Celtics?",
     "player_averages", {"player": "Nikola Jokić", "stats": ["reb"], "opponent": "BOS"}),
    ("Who leads the league in assists per game this season among players averaging 30+ minutes?",
     "league_leaders", {"stat": "ast", "min_minutes": 30, "season": "2025-26"}),
    # reworded and new questions
    ("Has Luka ever dropped 40 on the Clippers?",
     "game_count", {"player": "Luka Dončić", "conditions": {"pts": 40}, "opponent": "LAC"}),
    ("how many 30 pt games does luka doncic have vs LAL",
     "game_count", {"player": "Luka Dončić", "conditions": {"pts": 30}, "opponent": "LAL"}),
    ("How many triple-doubles does Jokic have this season?",
     "game_count", {"player": "Nikola Jokić", "conditions": {"pts": 10, "reb": 10, "ast": 10},
                    "season": "2025-26"}),
    ("How many games with 10+ assists and 10+ rebounds does Luka have on the road?",
     "game_count", {"player": "Luka Dončić", "conditions": {"ast": 10, "reb": 10},
                    "home_away": "away"}),
    ("Jokic worst free throw percentage season",
     "best_season", {"player": "Nikola Jokić", "stat": "ft_pct", "ascending": True}),
    ("What was Curry's best scoring season?",
     "best_season", {"player": "Stephen Curry", "stat": "pts"}),
    ("How many points does Curry average against Boston?",
     "player_averages", {"player": "Stephen Curry", "stats": ["pts"], "opponent": "BOS"}),
    ("Curry points at home in 2023-24",
     "player_averages", {"player": "Stephen Curry", "stats": ["pts"], "season": "2023-24",
                         "home_away": "home"}),
    ("Wembanyama blocks per game vs SAC",
     "player_averages", {"player": "Victor Wembanyama", "stats": ["blk"], "opponent": "SAC"}),
    ("Anthony Edwards stats",
     "player_averages", {"player": "Anthony Edwards", "stats": ["pts", "reb", "ast"]}),
    ("What is LeBron James's career scoring?",
     "career_stat", {"player": "LeBron James", "stats": ["pts"]}),
    ("Shai Gilgeous-Alexander career points",
     "career_stat", {"player": "Shai Gilgeous-Alexander", "stats": ["pts"]}),
    ("top 5 in 3P% with at least 200 3-point attempts",
     "league_leaders", {"stat": "fg3_pct", "min": {"fg3a": 200}, "limit": 5}),
    ("who has the most steals",
     "league_leaders", {"stat": "stl"}),
    ("Who scored the most total points this season?",
     "league_leaders", {"stat": "pts", "season": "2025-26", "per": "total"}),
    ("Which Lakers player leads in rebounds?",
     "league_leaders", {"stat": "reb", "team": "LAL"}),
    ("Career-high Steph Curry",
     "best_games", {"player": "Stephen Curry", "stat": "pts"}),
    ("Most points in a game Steph Curry",
     "best_games", {"player": "Stephen Curry", "stat": "pts"}),
    ("What's Jokic's career high in assists against the Lakers?",
     "best_games", {"player": "Nikola Jokić", "stat": "ast", "opponent": "LAL"}),
    ("Luka's top 3 single game rebound totals",
     "best_games", {"player": "Luka Dončić", "stat": "reb", "limit": 3}),
    # compare_players
    ("Curry vs Luka points per game",
     "compare_players", {"players": ["Stephen Curry", "Luka Dončić"], "stats": ["pts"]}),
    ("Compare Jokic and Embiid rebounds and assists",
     "compare_players", {"players": ["Nikola Jokić", "Joel Embiid"], "stats": ["reb", "ast"]}),
    ("Steph Curry vs Luka Doncic 3P% against the Lakers",
     "compare_players", {"players": ["Stephen Curry", "Luka Dončić"], "stats": ["fg3_pct"],
                         "opponent": "LAL"}),
    # record
    ("What's Luka's record when he scores 40+?",
     "record", {"player": "Luka Dončić", "conditions": {"pts": 40}}),
    ("Curry's record against the Lakers",
     "record", {"player": "Stephen Curry", "opponent": "LAL"}),
    ("Jokic win percentage in triple-doubles this season",
     "record", {"player": "Nikola Jokić", "conditions": {"pts": 10, "reb": 10, "ast": 10},
                "season": "2025-26"}),
    # streak
    ("Jokic's longest streak of triple-doubles",
     "streak", {"player": "Nikola Jokić", "conditions": {"pts": 10, "reb": 10, "ast": 10}}),
    ("How many 30 point games in a row has Luka had?",
     "streak", {"player": "Luka Dončić", "conditions": {"pts": 30}}),
    # recent_games
    ("Curry's last 10 games",
     "recent_games", {"player": "Stephen Curry", "n": 10}),
    ("How has Luka played lately?",
     "recent_games", {"player": "Luka Dončić", "n": 5}),
    ("Jokic rebounds last 5 games",
     "recent_games", {"player": "Nikola Jokić", "n": 5, "stats": ["reb"]}),
    # seasons
    ("Curry points last season",
     "player_averages", {"player": "Stephen Curry", "stats": ["pts"], "season": "2024-25"}),
    ("Who leads in assists last season?",
     "league_leaders", {"stat": "ast", "season": "2024-25"}),
    # things it should not try to answer
    ("what is the meaning of life", "unknown", {}),
    ("who is better, the heat or the magic", "unknown", {}),
]

# question -> text the answer must contain (needs a seeded courtside.db)
ANSWER_CASES = [
    ("Steph Curry's best 3-point shooting season with at least 400 attempts", "2018-19"),
    ("How many 30-point games does Luka have against the Lakers?", "has 8 games"),
    ("What are Jokić's rebound numbers against the Celtics?", "11.4 RPG"),
    ("Career-high Steph Curry", "most pts in a game is 62"),
    ("Curry vs Luka points per game", "PPG: Dončić 29.2 vs Curry 27.2"),
    ("What's Luka's record when he scores 40+?", "46-16"),
    ("Jokic's longest streak of triple-doubles", "is 5"),
    ("Curry's last 10 games", "last 10 games"),
    ("Who leads in assists last season?", "only loaded for 2025-26"),
    ("what is the meaning of life", "not capable"),
]


def test_parse_cases():
    failures = []
    for question, category, params in PARSE_CASES:
        got = parse(question)
        if got != (category, params):
            failures.append(f"{question!r}\n    expected {(category, params)}\n    got      {got}")
    assert not failures, "\n".join(failures)


def test_answer_cases():
    if not (ROOT / "courtside.db").exists():
        print("  skipped: courtside.db not found (run seed.py)")
        return
    for question, expected in ANSWER_CASES:
        answer = ask(question)["answer"]
        assert expected in answer, f"{question!r}: {answer!r} doesn't contain {expected!r}"


if __name__ == "__main__":
    failed = 0
    for test in (test_parse_cases, test_answer_cases):
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {test.__name__}\n{e}")
    sys.exit(1 if failed else 0)
