"""
Create and refresh materialized views for precomputed player aggregates.
Run: python refresh_aggregates.py
Requires: DATABASE_URL in environment or .env
"""

import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

MV_CAREER_TOTALS = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_player_career_totals AS
SELECT
    p.player_id,
    p.display_name,
    p.is_active,
    COUNT(*)                          AS games_played,
    SUM(s.pts)                        AS total_pts,
    ROUND(AVG(s.pts)::numeric, 1)     AS ppg,
    ROUND(AVG(s.reb)::numeric, 1)     AS rpg,
    ROUND(AVG(s.ast)::numeric, 1)     AS apg,
    ROUND(AVG(s.stl)::numeric, 1)     AS spg,
    ROUND(AVG(s.blk)::numeric, 1)     AS bpg,
    ROUND(
        CASE WHEN SUM(s.fga) > 0 THEN SUM(s.fgm)::numeric / SUM(s.fga) ELSE NULL END,
        3
    )                                  AS career_fg_pct,
    ROUND(
        CASE WHEN SUM(s.fg3a) > 0 THEN SUM(s.fg3m)::numeric / SUM(s.fg3a) ELSE NULL END,
        3
    )                                  AS career_fg3_pct,
    ROUND(
        CASE WHEN SUM(s.fta) > 0 THEN SUM(s.ftm)::numeric / SUM(s.fta) ELSE NULL END,
        3
    )                                  AS career_ft_pct,
    MAX(s.pts)                         AS career_high_pts,
    MAX(s.reb)                         AS career_high_reb,
    MAX(s.ast)                         AS career_high_ast
FROM player_game_stats s
JOIN players p USING (player_id)
GROUP BY p.player_id, p.display_name, p.is_active;
"""

MV_SEASON_AVERAGES = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_player_season_averages AS
SELECT
    p.player_id,
    p.display_name,
    s.season_id,
    COUNT(*)                          AS games_played,
    ROUND(AVG(s.pts)::numeric, 1)     AS ppg,
    ROUND(AVG(s.reb)::numeric, 1)     AS rpg,
    ROUND(AVG(s.ast)::numeric, 1)     AS apg,
    ROUND(AVG(s.stl)::numeric, 1)     AS spg,
    ROUND(AVG(s.blk)::numeric, 1)     AS bpg,
    ROUND(
        CASE WHEN SUM(s.fga) > 0 THEN SUM(s.fgm)::numeric / SUM(s.fga) ELSE NULL END,
        3
    )                                  AS fg_pct,
    ROUND(
        CASE WHEN SUM(s.fg3a) > 0 THEN SUM(s.fg3m)::numeric / SUM(s.fg3a) ELSE NULL END,
        3
    )                                  AS fg3_pct,
    ROUND(
        CASE WHEN SUM(s.fta) > 0 THEN SUM(s.ftm)::numeric / SUM(s.fta) ELSE NULL END,
        3
    )                                  AS ft_pct
FROM player_game_stats s
JOIN players p USING (player_id)
GROUP BY p.player_id, p.display_name, s.season_id;
"""

MV_MILESTONE_GAMES = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_player_milestone_games AS
SELECT
    p.player_id,
    p.display_name,
    COUNT(*) FILTER (WHERE s.pts >= 20) AS games_20_plus_pts,
    COUNT(*) FILTER (WHERE s.pts >= 30) AS games_30_plus_pts,
    COUNT(*) FILTER (WHERE s.pts >= 40) AS games_40_plus_pts,
    COUNT(*) FILTER (WHERE s.pts >= 50) AS games_50_plus_pts,
    COUNT(*) FILTER (WHERE s.pts >= 60) AS games_60_plus_pts,
    COUNT(*) FILTER (WHERE s.pts >= 10 AND s.reb >= 10 AND s.ast >= 10)
                                         AS triple_doubles,
    COUNT(*) FILTER (WHERE s.pts >= 10 AND s.reb >= 10)
                                         AS double_doubles_pts_reb,
    COUNT(*) FILTER (WHERE s.pts >= 10 AND s.ast >= 10)
                                         AS double_doubles_pts_ast
FROM player_game_stats s
JOIN players p USING (player_id)
GROUP BY p.player_id, p.display_name;
"""

MV_TEAM_BACK_TO_BACKS = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_team_back_to_backs AS
WITH team_games AS (
    SELECT DISTINCT team_id, game_id, game_date, season_id
    FROM player_game_stats
)
SELECT
    team_id, game_id, game_date, season_id,
    (game_date - LAG(game_date) OVER (PARTITION BY team_id ORDER BY game_date) = 1) AS is_b2b
FROM team_games;
"""

MV_PLAYER_PROP_HIT_RATES = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_player_prop_hit_rates AS
WITH recent_games AS (
    SELECT
        s.player_id,
        p.display_name,
        p.is_active,
        s.pts, s.reb, s.ast, s.fg3m,
        s.pts + s.reb + s.ast AS pra,
        ROW_NUMBER() OVER (PARTITION BY s.player_id ORDER BY s.game_date DESC) AS rn
    FROM player_game_stats s
    JOIN players p USING (player_id)
    WHERE s.season_id = '2024-25'
      AND p.is_active = true
)
SELECT
    player_id,
    display_name,
    -- Hit counts over last 10 games
    COUNT(*) FILTER (WHERE rn <= 10)                              AS games_last_10,
    COUNT(*) FILTER (WHERE rn <= 10 AND pts >= 15)                AS pts_15_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND pts >= 20)                AS pts_20_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND pts >= 25)                AS pts_25_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND pts >= 30)                AS pts_30_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND reb >= 6)                 AS reb_6_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND reb >= 8)                 AS reb_8_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND reb >= 10)                AS reb_10_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND ast >= 4)                 AS ast_4_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND ast >= 6)                 AS ast_6_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND ast >= 8)                 AS ast_8_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND fg3m >= 2)                AS fg3m_2_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND fg3m >= 3)                AS fg3m_3_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND fg3m >= 4)                AS fg3m_4_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND pra >= 30)                AS pra_30_hit_last10,
    COUNT(*) FILTER (WHERE rn <= 10 AND pra >= 40)                AS pra_40_hit_last10,
    -- Hit counts over last 20 games
    COUNT(*) FILTER (WHERE rn <= 20)                              AS games_last_20,
    COUNT(*) FILTER (WHERE rn <= 20 AND pts >= 15)                AS pts_15_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND pts >= 20)                AS pts_20_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND pts >= 25)                AS pts_25_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND pts >= 30)                AS pts_30_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND reb >= 6)                 AS reb_6_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND reb >= 8)                 AS reb_8_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND reb >= 10)                AS reb_10_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND ast >= 4)                 AS ast_4_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND ast >= 6)                 AS ast_6_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND ast >= 8)                 AS ast_8_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND fg3m >= 2)                AS fg3m_2_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND fg3m >= 3)                AS fg3m_3_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND fg3m >= 4)                AS fg3m_4_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND pra >= 30)                AS pra_30_hit_last20,
    COUNT(*) FILTER (WHERE rn <= 20 AND pra >= 40)                AS pra_40_hit_last20,
    -- Last 10 averages and consistency
    ROUND(AVG(pts) FILTER (WHERE rn <= 10)::numeric, 1)          AS avg_pts_last10,
    ROUND(STDDEV(pts) FILTER (WHERE rn <= 10)::numeric, 1)       AS stddev_pts_last10,
    MIN(pts) FILTER (WHERE rn <= 10)                              AS min_pts_last10,
    MAX(pts) FILTER (WHERE rn <= 10)                              AS max_pts_last10,
    ROUND(AVG(reb) FILTER (WHERE rn <= 10)::numeric, 1)          AS avg_reb_last10,
    ROUND(STDDEV(reb) FILTER (WHERE rn <= 10)::numeric, 1)       AS stddev_reb_last10,
    MIN(reb) FILTER (WHERE rn <= 10)                              AS min_reb_last10,
    MAX(reb) FILTER (WHERE rn <= 10)                              AS max_reb_last10,
    ROUND(AVG(ast) FILTER (WHERE rn <= 10)::numeric, 1)          AS avg_ast_last10,
    ROUND(STDDEV(ast) FILTER (WHERE rn <= 10)::numeric, 1)       AS stddev_ast_last10,
    MIN(ast) FILTER (WHERE rn <= 10)                              AS min_ast_last10,
    MAX(ast) FILTER (WHERE rn <= 10)                              AS max_ast_last10,
    ROUND(AVG(fg3m) FILTER (WHERE rn <= 10)::numeric, 1)         AS avg_fg3m_last10,
    ROUND(AVG(pra) FILTER (WHERE rn <= 10)::numeric, 1)          AS avg_pra_last10
FROM recent_games
WHERE rn <= 20
GROUP BY player_id, display_name;
"""

MV_PLAYER_HOME_AWAY_SPLITS = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_player_home_away_splits AS
SELECT
    s.player_id,
    p.display_name,
    CASE WHEN s.matchup LIKE '%vs.%' THEN 'Home' ELSE 'Away' END AS location,
    COUNT(*)                                      AS games,
    ROUND(AVG(s.pts)::numeric, 1)                 AS ppg,
    ROUND(AVG(s.reb)::numeric, 1)                 AS rpg,
    ROUND(AVG(s.ast)::numeric, 1)                 AS apg,
    ROUND(AVG(s.fg3m)::numeric, 1)                AS fg3mpg,
    ROUND(STDDEV(s.pts)::numeric, 1)              AS stddev_pts,
    ROUND(
        CASE WHEN SUM(s.fga) > 0 THEN SUM(s.fgm)::numeric / SUM(s.fga) ELSE NULL END,
        3
    )                                              AS fg_pct
FROM player_game_stats s
JOIN players p USING (player_id)
WHERE s.season_id = '2024-25'
GROUP BY s.player_id, p.display_name,
         CASE WHEN s.matchup LIKE '%vs.%' THEN 'Home' ELSE 'Away' END;
"""

MV_TEAM_DEFENSIVE_RATINGS = """
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_team_defensive_ratings AS
WITH opponent_stats AS (
    SELECT
        CASE
            WHEN s.matchup LIKE '%@%' THEN
                SPLIT_PART(REPLACE(s.matchup, s.team_abbr || ' @ ', ''), ' ', 1)
            ELSE
                SPLIT_PART(REPLACE(s.matchup, s.team_abbr || ' vs. ', ''), ' ', 1)
        END AS opponent_abbr,
        s.pts, s.reb, s.ast, s.fg3m
    FROM player_game_stats s
    WHERE s.season_id = '2024-25'
)
SELECT
    t.team_id,
    t.abbreviation AS team_abbr,
    t.full_name AS team_name,
    COUNT(*)                                       AS games_against,
    ROUND(AVG(o.pts)::numeric, 1)                  AS opp_ppg_allowed,
    ROUND(AVG(o.reb)::numeric, 1)                  AS opp_rpg_allowed,
    ROUND(AVG(o.ast)::numeric, 1)                  AS opp_apg_allowed,
    ROUND(AVG(o.fg3m)::numeric, 1)                 AS opp_fg3mpg_allowed
FROM opponent_stats o
JOIN teams t ON t.abbreviation = o.opponent_abbr
GROUP BY t.team_id, t.abbreviation, t.full_name;
"""

UNIQUE_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_pct_pk ON mv_player_career_totals (player_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_psa_pk ON mv_player_season_averages (player_id, season_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_pmg_pk ON mv_player_milestone_games (player_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_tb2b_pk ON mv_team_back_to_backs (team_id, game_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_pphr_pk ON mv_player_prop_hit_rates (player_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_phas_pk ON mv_player_home_away_splits (player_id, location);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_tdr_pk ON mv_team_defensive_ratings (team_id);",
]

VIEWS = [
    "mv_player_career_totals",
    "mv_player_season_averages",
    "mv_player_milestone_games",
    "mv_team_back_to_backs",
    "mv_player_prop_hit_rates",
    "mv_player_home_away_splits",
    "mv_team_defensive_ratings",
]


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "Set DATABASE_URL (e.g. postgresql://user:password@localhost:5432/nba). "
            "You can put it in a .env file."
        )

    conn = psycopg2.connect(database_url)
    conn.autocommit = True

    with conn.cursor() as cur:
        # Create materialized views if they don't exist
        print("Creating materialized views (if needed) ...")
        cur.execute(MV_CAREER_TOTALS)
        cur.execute(MV_SEASON_AVERAGES)
        cur.execute(MV_MILESTONE_GAMES)
        cur.execute(MV_TEAM_BACK_TO_BACKS)
        cur.execute(MV_PLAYER_PROP_HIT_RATES)
        cur.execute(MV_PLAYER_HOME_AWAY_SPLITS)
        cur.execute(MV_TEAM_DEFENSIVE_RATINGS)

        # Create unique indexes for CONCURRENTLY refresh
        for idx_sql in UNIQUE_INDEXES:
            cur.execute(idx_sql)

        # Refresh concurrently
        for view in VIEWS:
            print(f"  Refreshing {view} ...", flush=True)
            cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view};")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
