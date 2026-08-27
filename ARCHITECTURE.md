System Components
1. Discourse engine — would scrape data from Reddit to train the model on what
fans are curious about. Turns reddit data into simple questions. For instance,
if fans are remarking about how old superstars are in the NBA nowadays, it will
be able to generate insightful questions (what's the average age of an all-NBA
player nowadays?).
2a. Data layer — courtside.db, seeded from nba.api and Basketball Reference.
Stores game logs, league stats, and awards data. Will update as more queries are
made.
2b. SQL inquiries — turns questions into quantifiable answers and tables after
reading courtside.db. This is most similar to statmuse, as it takes key words
and translates them into code queries. An LLM will classify the intent of the
query (best season, game count, statline) and SQL will handle the query
3. Frontend.

Data flow:
Reddit → [Discourse engine] → generates questions
Questions → [Query engine] → SQL → courtside.db → answer
Answer → [Frontend] → displayed to user

Intent categories: best_season, game_count, player_averages, league_leaders, career_stat