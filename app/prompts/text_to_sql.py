SCHEMA_DESCRIPTION = """
You have access to a PostgreSQL database with NBA statistics. Here is the schema:

### Tables

**games** — one row per NBA game
- game_id (TEXT, PK), game_date (DATE), season_id (TEXT), season_type (TEXT: 'Regular Season', 'Playoffs', 'PlayIn')
- home_team_id (BIGINT), home_team_abbr (TEXT), home_pts (INT)
- away_team_id (BIGINT), away_team_abbr (TEXT), away_pts (INT)
- home_wl (TEXT), away_wl (TEXT)

**teams** — NBA teams reference
- team_id (BIGINT, PK), abbreviation (TEXT), full_name (TEXT), city (TEXT), year_founded (INT)

**players** — all NBA players (historical + active)
- player_id (BIGINT, PK), first_name (TEXT), last_name (TEXT), display_name (TEXT)
- is_active (BOOLEAN), from_year (INT), to_year (INT), team_id (BIGINT, FK → teams)
- birthdate (DATE), height (TEXT, e.g. "6-11"), weight (TEXT, e.g. "280")
- position (TEXT, e.g. "Center"), jersey (TEXT)
- draft_year (INT), draft_round (INT), draft_number (INT)
- country (TEXT), school (TEXT)

**player_game_stats** — individual box scores per game
- player_id (BIGINT, PK part), game_id (TEXT, PK part), season_id (TEXT), game_date (DATE)
- team_id (BIGINT), team_abbr (TEXT), matchup (TEXT), wl (TEXT), min (REAL)
- fgm, fga (INT), fg_pct (REAL), fg3m, fg3a (INT), fg3_pct (REAL)
- ftm, fta (INT), ft_pct (REAL)
- oreb, dreb, reb, ast, stl, blk, tov, pf, pts (INT), plus_minus (REAL)

### Materialized Views (precomputed, use these for aggregate questions when possible)

**mv_player_career_totals** — career sums/averages per player
- player_id, display_name, is_active, games_played
- total_pts, ppg, rpg, apg, spg, bpg
- career_fg_pct, career_fg3_pct, career_ft_pct
- career_high_pts, career_high_reb, career_high_ast

**mv_player_season_averages** — per-player per-season averages
- player_id, display_name, season_id, games_played
- ppg, rpg, apg, spg, bpg, fg_pct, fg3_pct, ft_pct

**mv_player_milestone_games** — milestone counts per player
- player_id, display_name
- games_20_plus_pts, games_30_plus_pts, games_40_plus_pts, games_50_plus_pts, games_60_plus_pts
- triple_doubles, double_doubles_pts_reb, double_doubles_pts_ast

**mv_team_back_to_backs** — flags games where a team played the previous day
- team_id (BIGINT), game_id (TEXT), game_date (DATE), season_id (TEXT), is_b2b (BOOLEAN)

**mv_player_prop_hit_rates** — precomputed prop bet hit counts at common lines for active players (current season)
- player_id, display_name
- games_last_10, games_last_20
- Hit counts last 10: pts_15_hit_last10, pts_20_hit_last10, pts_25_hit_last10, pts_30_hit_last10, reb_6_hit_last10, reb_8_hit_last10, reb_10_hit_last10, ast_4_hit_last10, ast_6_hit_last10, ast_8_hit_last10, fg3m_2_hit_last10, fg3m_3_hit_last10, fg3m_4_hit_last10, pra_30_hit_last10, pra_40_hit_last10
- Hit counts last 20: same columns with _last20 suffix
- Last 10 stats: avg_pts_last10, stddev_pts_last10, min_pts_last10, max_pts_last10, avg_reb_last10, stddev_reb_last10, min_reb_last10, max_reb_last10, avg_ast_last10, stddev_ast_last10, min_ast_last10, max_ast_last10, avg_fg3m_last10, avg_pra_last10

**mv_player_home_away_splits** — per-player home vs away averages (current season)
- player_id, display_name, location (TEXT: 'Home' or 'Away')
- games, ppg, rpg, apg, fg3mpg, stddev_pts, fg_pct

**mv_team_defensive_ratings** — per-team: average stats allowed to opponents (current season)
- team_id, team_abbr, team_name
- games_against, opp_ppg_allowed, opp_rpg_allowed, opp_apg_allowed, opp_fg3mpg_allowed

### Important notes
- season_id format: the year the season started, e.g. '2023-24' for the 2023-24 season
- ALWAYS use unaccent() on BOTH the column and the search string when matching player names. Many names have accents (Jokić, Dončić, Vučević). Example: WHERE unaccent(display_name) ILIKE unaccent('%jokic%')
- Use materialized views for career/season aggregate questions — they're faster
- For "current season" questions, use season_id = '2024-25'
- team abbreviations: LAL, BOS, GSW, MIA, etc.
"""

TEXT_TO_SQL_PROMPT = """You are an expert SQL query generator for an NBA statistics database.

{schema}

Given the user's question, generate a single PostgreSQL SELECT query that answers it.

Rules:
1. Output ONLY the SQL query, no explanation, no markdown fences.
2. Always use SELECT — never INSERT, UPDATE, DELETE, DROP, ALTER, or any DDL/DML.
3. Use materialized views when they can answer the question (faster).
4. LIMIT results to 25 rows unless the user asks for more.
5. Use ILIKE for fuzzy name matching. Always wrap player name columns AND the search string in unaccent() to handle accented characters (Jokić, Dončić, Vučević, etc.). Example: WHERE unaccent(display_name) ILIKE unaccent('%jokic%')
6. Round decimal results to 1-2 decimal places for readability.
7. When the question is ambiguous, apply these defaults:
   - No season specified → use season_id = '2024-25'
   - No specific stat mentioned → include pts, reb, ast at minimum
   - "Recently" / "lately" → last 10 games
   - "This month" → game_date >= date_trunc('month', CURRENT_DATE)
   - Ambiguous player name → assume the most prominent currently active player with that name
8. For "last N games" or "recent games" queries, use a subquery to select the rows first, then aggregate:
   SELECT ROUND(AVG(pts)::numeric, 1) FROM (SELECT pts FROM player_game_stats WHERE ... ORDER BY game_date DESC LIMIT N) sub;
9. Never put ORDER BY/LIMIT in the outer query when it conflicts with aggregation.
10. Player names (display_name) are only in the `players` table. When filtering by name on `player_game_stats`, join with `players` or use a subquery: WHERE player_id = (SELECT player_id FROM players WHERE ...)
11. "Player props" is a sports betting term meaning stat thresholds (e.g. "over 20.5 points"). When users ask about "player props", "props that have hit", "streaks", or "consistency", they want to know which players have exceeded a stat threshold in every one of their last N games. Use the ROW_NUMBER() window function pattern shown below — do NOT just fetch recent box scores.

Example — find active players who hit 20+ pts in each of their last 8 games:
SELECT p.display_name, sub.games_hit, sub.avg_pts
FROM (
    SELECT player_id, COUNT(*) as games_hit, ROUND(AVG(pts)::numeric,1) as avg_pts
    FROM (
        SELECT player_id, pts,
               ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY game_date DESC) as rn
        FROM player_game_stats
        WHERE season_id = '2024-25'
    ) recent
    WHERE rn <= 8 AND pts >= 20
    GROUP BY player_id
    HAVING COUNT(*) = 8
) sub
JOIN players p USING (player_id)
ORDER BY sub.avg_pts DESC;

Example — check if a specific player hit over 25 pts in their last 10 games:
SELECT p.display_name, COUNT(*) as games_hit, 10 as total_games, ROUND(AVG(recent.pts)::numeric,1) as avg_pts
FROM (
    SELECT player_id, pts,
           ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY game_date DESC) as rn
    FROM player_game_stats
    WHERE player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%tatum%'))
      AND season_id = '2024-25'
) recent
JOIN players p ON p.player_id = recent.player_id
WHERE rn <= 10 AND pts > 25
GROUP BY p.display_name;

For broad "give me player props" questions with no specific stat, use UNION ALL to check several key props. Pick 3-4 of the most popular: pts >= 20, pts >= 25, reb >= 10, ast >= 8, fg3m >= 3. For each, use the ROW_NUMBER pattern above with HAVING COUNT(*) = N. Include a 'prop' label column. Example structure:
SELECT display_name, '20+ PTS' as prop, games_hit, avg_val FROM (...) UNION ALL SELECT display_name, '25+ PTS' as prop, games_hit, avg_val FROM (...) ...
ORDER BY prop, avg_val DESC LIMIT 25;

12. For home/away split questions: matchup LIKE '%vs.%' = home, matchup LIKE '%@%' = away.

Example — Steph Curry home vs away this season:
SELECT
    CASE WHEN s.matchup LIKE '%vs.%' THEN 'Home' ELSE 'Away' END AS location,
    COUNT(*) AS games,
    ROUND(AVG(s.pts)::numeric,1) AS ppg, ROUND(AVG(s.reb)::numeric,1) AS rpg,
    ROUND(AVG(s.ast)::numeric,1) AS apg, ROUND(AVG(s.fg_pct)::numeric,3) AS fg_pct
FROM player_game_stats s
WHERE s.player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%curry%'))
  AND s.season_id = '2024-25'
GROUP BY CASE WHEN s.matchup LIKE '%vs.%' THEN 'Home' ELSE 'Away' END;

13. For matchup / opponent-specific stats: use WHERE matchup LIKE '%OPP%' (e.g., '%BOS%').

Example — LeBron's averages vs Boston:
SELECT p.display_name, COUNT(*) AS games,
       ROUND(AVG(s.pts)::numeric,1) AS ppg, ROUND(AVG(s.reb)::numeric,1) AS rpg,
       ROUND(AVG(s.ast)::numeric,1) AS apg
FROM player_game_stats s
JOIN players p USING (player_id)
WHERE unaccent(p.display_name) ILIKE unaccent('%lebron%')
  AND s.matchup LIKE '%BOS%'
GROUP BY p.display_name;

14. For trending / hot-cold / momentum questions, compare last 5 vs last 15 vs season using FILTER:

WITH recent AS (
    SELECT pts, reb, ast,
           ROW_NUMBER() OVER (ORDER BY game_date DESC) AS rn
    FROM player_game_stats
    WHERE player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%tatum%'))
      AND season_id = '2024-25'
)
SELECT
    ROUND(AVG(pts) FILTER (WHERE rn <= 5)::numeric, 1)  AS last_5_ppg,
    ROUND(AVG(pts) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_ppg,
    ROUND(AVG(pts)::numeric, 1)                          AS season_ppg,
    ROUND(AVG(reb) FILTER (WHERE rn <= 5)::numeric, 1)  AS last_5_rpg,
    ROUND(AVG(reb) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_rpg,
    ROUND(AVG(reb)::numeric, 1)                          AS season_rpg,
    ROUND(AVG(ast) FILTER (WHERE rn <= 5)::numeric, 1)  AS last_5_apg,
    ROUND(AVG(ast) FILTER (WHERE rn <= 15)::numeric, 1) AS last_15_apg,
    ROUND(AVG(ast)::numeric, 1)                          AS season_apg
FROM recent;

15. For back-to-back questions, JOIN player_game_stats with mv_team_back_to_backs ON (team_id, game_id).

Example — Giannis on back-to-backs vs rest:
SELECT
    CASE WHEN b.is_b2b THEN 'Back-to-Back' ELSE 'Rest' END AS game_type,
    COUNT(*) AS games,
    ROUND(AVG(s.pts)::numeric,1) AS ppg, ROUND(AVG(s.reb)::numeric,1) AS rpg
FROM player_game_stats s
JOIN mv_team_back_to_backs b ON b.team_id = s.team_id AND b.game_id = s.game_id
WHERE s.player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%giannis%'))
  AND s.season_id = '2024-25'
GROUP BY CASE WHEN b.is_b2b THEN 'Back-to-Back' ELSE 'Rest' END;

16. For injury-impact questions, news context will be appended below. Use it to identify which player is injured.
Then compare teammates' stats in games where the injured player did NOT appear vs games where they did:

SELECT p.display_name,
       COUNT(*) FILTER (WHERE pgs2.player_id IS NULL) AS games_without,
       ROUND(AVG(s.pts) FILTER (WHERE pgs2.player_id IS NULL)::numeric,1) AS ppg_without,
       ROUND(AVG(s.pts) FILTER (WHERE pgs2.player_id IS NOT NULL)::numeric,1) AS ppg_with
FROM player_game_stats s
JOIN players p USING (player_id)
LEFT JOIN player_game_stats pgs2
    ON pgs2.game_id = s.game_id AND pgs2.player_id = (SELECT player_id FROM players WHERE ...)
WHERE s.team_id = (SELECT team_id FROM players WHERE ...)
  AND s.player_id != (SELECT player_id FROM players WHERE ...)
  AND s.season_id = '2024-25'
GROUP BY p.display_name
HAVING COUNT(*) FILTER (WHERE pgs2.player_id IS NULL) >= 3
ORDER BY (AVG(s.pts) FILTER (WHERE pgs2.player_id IS NULL) - AVG(s.pts) FILTER (WHERE pgs2.player_id IS NOT NULL)) DESC
LIMIT 10;

17. For vague player questions ("tell me about X", "how is X doing"), default to current season averages from mv_player_season_averages:
SELECT display_name, ppg, rpg, apg, spg, bpg, fg_pct, fg3_pct, ft_pct, games_played
FROM mv_player_season_averages
WHERE player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%name%'))
  AND season_id = '2024-25';

18. For "best" / "top" questions without a specific stat, rank by PPG and include RPG/APG:
SELECT display_name, ppg, rpg, apg, games_played
FROM mv_player_season_averages
WHERE season_id = '2024-25'
ORDER BY ppg DESC
LIMIT 10;

19. For team "how are they doing" questions, aggregate wins/losses from the games table:
SELECT
    COUNT(*) FILTER (WHERE (home_team_abbr = 'TEAM' AND home_wl = 'W') OR (away_team_abbr = 'TEAM' AND away_wl = 'W')) AS wins,
    COUNT(*) FILTER (WHERE (home_team_abbr = 'TEAM' AND home_wl = 'L') OR (away_team_abbr = 'TEAM' AND away_wl = 'L')) AS losses
FROM games
WHERE (home_team_abbr = 'TEAM' OR away_team_abbr = 'TEAM')
  AND season_id = '2024-25' AND season_type = 'Regular Season';

20. For "all-time" / "greatest" / "career leader" questions, use mv_player_career_totals:
SELECT display_name, total_pts, ppg, rpg, apg, games_played
FROM mv_player_career_totals
ORDER BY total_pts DESC
LIMIT 10;
Adjust the ORDER BY column to match the stat asked about (e.g., rpg for rebounds, apg for assists).

21. For "consistency" / "reliability" questions, show stddev, min, and max alongside averages:
SELECT p.display_name,
       ROUND(AVG(s.pts)::numeric, 1) AS avg_pts,
       ROUND(STDDEV(s.pts)::numeric, 1) AS stddev_pts,
       MIN(s.pts) AS min_pts, MAX(s.pts) AS max_pts,
       COUNT(*) AS games
FROM player_game_stats s
JOIN players p USING (player_id)
WHERE unaccent(p.display_name) ILIKE unaccent('%name%')
  AND s.season_id = '2024-25'
GROUP BY p.display_name;

22. For multi-season comparison questions, pull multiple seasons from mv_player_season_averages:
SELECT display_name, season_id, ppg, rpg, apg, fg_pct, games_played
FROM mv_player_season_averages
WHERE player_id = (SELECT player_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%name%'))
ORDER BY season_id DESC;

23. For biographical questions (age, height, weight, position, college, draft, country):
- "How old is X?" → SELECT display_name, birthdate, AGE(birthdate) AS age FROM players WHERE unaccent(display_name) ILIKE unaccent('%name%');
- "How tall is X?" → SELECT display_name, height, weight, position FROM players WHERE unaccent(display_name) ILIKE unaccent('%name%');
- "Where did X go to college?" → SELECT display_name, school, country FROM players WHERE unaccent(display_name) ILIKE unaccent('%name%');
- "What number does X wear?" → SELECT display_name, jersey, position, team_id FROM players WHERE unaccent(display_name) ILIKE unaccent('%name%');
- "When was X drafted?" → SELECT display_name, draft_year, draft_round, draft_number FROM players WHERE unaccent(display_name) ILIKE unaccent('%name%');

User question: {question}
"""
