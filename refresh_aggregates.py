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

UNIQUE_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_pct_pk ON mv_player_career_totals (player_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_psa_pk ON mv_player_season_averages (player_id, season_id);",
    "CREATE UNIQUE INDEX IF NOT EXISTS mv_pmg_pk ON mv_player_milestone_games (player_id);",
]

VIEWS = [
    "mv_player_career_totals",
    "mv_player_season_averages",
    "mv_player_milestone_games",
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
