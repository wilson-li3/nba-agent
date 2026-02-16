"""
Bulk-load player box scores into player_game_stats using PlayerGameLogs endpoint.
One API call per season (instead of per-player), covering Regular Season and Playoffs.
Run: python sync_player_stats.py
Requires: DATABASE_URL in environment or .env
"""

import os
import time

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from nba_api.stats.endpoints import playergamelogs

load_dotenv()

FIRST_SEASON = "1946-47"
CURRENT_SEASON = "2024-25"
REQUEST_DELAY_SEC = 0.6


def get_season_list() -> list[str]:
    start_year = int(FIRST_SEASON.split("-")[0])
    end_year = int(CURRENT_SEASON.split("-")[0])
    return [f"{y}-{str(y + 1)[-2:]}" for y in range(start_year, end_year + 1)]


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS player_game_stats (
                player_id  BIGINT NOT NULL,
                game_id    TEXT NOT NULL,
                season_id  TEXT NOT NULL,
                game_date  DATE NOT NULL,
                team_id    BIGINT,
                team_abbr  TEXT,
                matchup    TEXT,
                wl         TEXT,
                min        REAL,
                fgm INT, fga INT, fg_pct REAL,
                fg3m INT, fg3a INT, fg3_pct REAL,
                ftm INT, fta INT, ft_pct REAL,
                oreb INT, dreb INT, reb INT,
                ast INT, stl INT, blk INT, tov INT, pf INT, pts INT,
                plus_minus REAL,
                PRIMARY KEY (player_id, game_id)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pgs_game_date
            ON player_game_stats (game_date);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pgs_season
            ON player_game_stats (season_id);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_pgs_pts
            ON player_game_stats (pts);
        """)
        conn.commit()


def safe_int(val):
    if pd.isna(val):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def safe_float(val):
    if pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def fetch_and_upsert(conn, season: str, season_type: str) -> int:
    """Fetch all player game logs for one season/type and upsert into DB."""
    time.sleep(REQUEST_DELAY_SEC)
    try:
        logs = playergamelogs.PlayerGameLogs(
            season_nullable=season,
            season_type_nullable=season_type,
        )
        df = logs.get_data_frames()[0]
    except Exception as e:
        print(f"  API error for {season} {season_type}: {e}")
        return 0

    if df is None or df.empty:
        return 0

    with conn.cursor() as cur:
        for _, row in df.iterrows():
            cur.execute("""
                INSERT INTO player_game_stats (
                    player_id, game_id, season_id, game_date,
                    team_id, team_abbr, matchup, wl, min,
                    fgm, fga, fg_pct, fg3m, fg3a, fg3_pct,
                    ftm, fta, ft_pct,
                    oreb, dreb, reb, ast, stl, blk, tov, pf, pts,
                    plus_minus
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s
                )
                ON CONFLICT (player_id, game_id) DO UPDATE SET
                    season_id = EXCLUDED.season_id,
                    game_date = EXCLUDED.game_date,
                    team_id = EXCLUDED.team_id,
                    team_abbr = EXCLUDED.team_abbr,
                    matchup = EXCLUDED.matchup,
                    wl = EXCLUDED.wl,
                    min = EXCLUDED.min,
                    fgm = EXCLUDED.fgm, fga = EXCLUDED.fga, fg_pct = EXCLUDED.fg_pct,
                    fg3m = EXCLUDED.fg3m, fg3a = EXCLUDED.fg3a, fg3_pct = EXCLUDED.fg3_pct,
                    ftm = EXCLUDED.ftm, fta = EXCLUDED.fta, ft_pct = EXCLUDED.ft_pct,
                    oreb = EXCLUDED.oreb, dreb = EXCLUDED.dreb, reb = EXCLUDED.reb,
                    ast = EXCLUDED.ast, stl = EXCLUDED.stl, blk = EXCLUDED.blk,
                    tov = EXCLUDED.tov, pf = EXCLUDED.pf, pts = EXCLUDED.pts,
                    plus_minus = EXCLUDED.plus_minus
            """, (
                safe_int(row.get("PLAYER_ID")),
                str(row.get("GAME_ID", "")),
                str(row.get("SEASON_YEAR", season)),
                pd.to_datetime(row.get("GAME_DATE")).date() if pd.notna(row.get("GAME_DATE")) else None,
                safe_int(row.get("TEAM_ID")),
                str(row.get("TEAM_ABBREVIATION", "")) if pd.notna(row.get("TEAM_ABBREVIATION")) else None,
                str(row.get("MATCHUP", "")) if pd.notna(row.get("MATCHUP")) else None,
                str(row.get("WL", "")) if pd.notna(row.get("WL")) else None,
                safe_float(row.get("MIN")),
                safe_int(row.get("FGM")), safe_int(row.get("FGA")), safe_float(row.get("FG_PCT")),
                safe_int(row.get("FG3M")), safe_int(row.get("FG3A")), safe_float(row.get("FG3_PCT")),
                safe_int(row.get("FTM")), safe_int(row.get("FTA")), safe_float(row.get("FT_PCT")),
                safe_int(row.get("OREB")), safe_int(row.get("DREB")), safe_int(row.get("REB")),
                safe_int(row.get("AST")), safe_int(row.get("STL")), safe_int(row.get("BLK")),
                safe_int(row.get("TOV")), safe_int(row.get("PF")), safe_int(row.get("PTS")),
                safe_float(row.get("PLUS_MINUS")),
            ))
        conn.commit()
    return len(df)


def main() -> None:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "Set DATABASE_URL (e.g. postgresql://user:password@localhost:5432/nba). "
            "You can put it in a .env file."
        )

    conn = psycopg2.connect(database_url)
    ensure_schema(conn)

    seasons = get_season_list()
    total = 0

    for i, season in enumerate(seasons):
        print(f"[{i+1}/{len(seasons)}] {season} ...", end=" ", flush=True)

        n_reg = fetch_and_upsert(conn, season, "Regular Season")
        print(f"Regular: {n_reg}", end=" ")

        n_po = fetch_and_upsert(conn, season, "Playoffs")
        print(f"Playoffs: {n_po}")

        total += n_reg + n_po

    conn.close()
    print(f"Done. Total rows upserted: {total}")


if __name__ == "__main__":
    main()
