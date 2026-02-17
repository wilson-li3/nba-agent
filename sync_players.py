"""
Populate the teams and players tables from the NBA API.
Run: python sync_players.py          # teams + players only
     python sync_players.py --bios   # also sync biographical data for active players
Requires: DATABASE_URL in environment or .env
"""

import argparse
import os
import time

import psycopg2
from dotenv import load_dotenv
from nba_api.stats.static import teams as static_teams
from nba_api.stats.endpoints import commonallplayers, commonplayerinfo

load_dotenv()

REQUEST_DELAY_SEC = 0.6


def ensure_schema(conn) -> None:
    """Create teams and players tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id       BIGINT PRIMARY KEY,
                abbreviation  TEXT NOT NULL,
                full_name     TEXT NOT NULL,
                city          TEXT,
                year_founded  INT
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS players (
                player_id    BIGINT PRIMARY KEY,
                first_name   TEXT NOT NULL,
                last_name    TEXT NOT NULL,
                display_name TEXT NOT NULL,
                is_active    BOOLEAN DEFAULT FALSE,
                from_year    INT,
                to_year      INT,
                team_id      BIGINT REFERENCES teams(team_id)
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_players_name
            ON players (LOWER(display_name));
        """)
        # Bio columns — safe to re-run (ADD COLUMN IF NOT EXISTS)
        bio_columns = [
            ("birthdate", "DATE"),
            ("height", "TEXT"),
            ("weight", "TEXT"),
            ("position", "TEXT"),
            ("jersey", "TEXT"),
            ("draft_year", "INT"),
            ("draft_round", "INT"),
            ("draft_number", "INT"),
            ("country", "TEXT"),
            ("school", "TEXT"),
        ]
        for col_name, col_type in bio_columns:
            cur.execute(f"ALTER TABLE players ADD COLUMN IF NOT EXISTS {col_name} {col_type};")
        conn.commit()


def sync_teams(conn) -> int:
    """Load all NBA teams from static data (no API call needed)."""
    all_teams = static_teams.get_teams()
    with conn.cursor() as cur:
        for t in all_teams:
            cur.execute("""
                INSERT INTO teams (team_id, abbreviation, full_name, city, year_founded)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (team_id) DO UPDATE SET
                    abbreviation = EXCLUDED.abbreviation,
                    full_name = EXCLUDED.full_name,
                    city = EXCLUDED.city,
                    year_founded = EXCLUDED.year_founded
            """, (
                t["id"], t["abbreviation"], t["full_name"],
                t["city"], t["year_founded"],
            ))
        conn.commit()
    return len(all_teams)


def sync_players(conn) -> int:
    """Load all NBA players (historical + active) via CommonAllPlayers endpoint."""
    time.sleep(REQUEST_DELAY_SEC)
    result = commonallplayers.CommonAllPlayers(is_only_current_season=0)
    df = result.get_data_frames()[0]

    if df is None or df.empty:
        print("No player data returned.")
        return 0

    count = 0
    with conn.cursor() as cur:
        for _, row in df.iterrows():
            player_id = int(row["PERSON_ID"])
            display_name = str(row["DISPLAY_FIRST_LAST"]).strip()
            parts = display_name.split(" ", 1)
            first_name = parts[0] if parts else ""
            last_name = parts[1] if len(parts) > 1 else ""

            # Parse from/to years
            from_year = None
            to_year = None
            if "FROM_YEAR" in row and row["FROM_YEAR"]:
                try:
                    from_year = int(row["FROM_YEAR"])
                except (ValueError, TypeError):
                    pass
            if "TO_YEAR" in row and row["TO_YEAR"]:
                try:
                    to_year = int(row["TO_YEAR"])
                except (ValueError, TypeError):
                    pass

            # Determine team_id (0 means no team)
            team_id = None
            if "TEAM_ID" in row and row["TEAM_ID"] and int(row["TEAM_ID"]) != 0:
                team_id = int(row["TEAM_ID"])

            # Determine active status
            is_active = False
            if "ROSTERSTATUS" in row:
                is_active = row["ROSTERSTATUS"] == 1 or row["ROSTERSTATUS"] == "1"

            cur.execute("""
                INSERT INTO players (player_id, first_name, last_name, display_name,
                                     is_active, from_year, to_year, team_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (player_id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    display_name = EXCLUDED.display_name,
                    is_active = EXCLUDED.is_active,
                    from_year = EXCLUDED.from_year,
                    to_year = EXCLUDED.to_year,
                    team_id = EXCLUDED.team_id
            """, (
                player_id, first_name, last_name, display_name,
                is_active, from_year, to_year, team_id,
            ))
            count += 1

        conn.commit()
    return count


def sync_player_bios(conn) -> int:
    """Fetch biographical info for active players via CommonPlayerInfo endpoint."""
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM players WHERE is_active = TRUE")
        active_ids = [row[0] for row in cur.fetchall()]

    if not active_ids:
        print("No active players found — skipping bios.")
        return 0

    count = 0
    total = len(active_ids)
    for i, pid in enumerate(active_ids):
        try:
            time.sleep(REQUEST_DELAY_SEC)
            info = commonplayerinfo.CommonPlayerInfo(player_id=pid)
            df = info.get_data_frames()[0]
            if df is None or df.empty:
                continue
            row = df.iloc[0]

            birthdate = row.get("BIRTHDATE")
            if birthdate and str(birthdate).strip():
                # The API returns ISO format; keep just the date part
                birthdate = str(birthdate).strip()[:10]
            else:
                birthdate = None

            def _safe_int(val):
                try:
                    v = int(val)
                    return v if v != 0 else None
                except (ValueError, TypeError):
                    return None

            def _safe_str(val):
                s = str(val).strip() if val else None
                return s if s and s.lower() != "nan" else None

            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE players SET
                        birthdate   = %s,
                        height      = %s,
                        weight      = %s,
                        position    = %s,
                        jersey      = %s,
                        draft_year  = %s,
                        draft_round = %s,
                        draft_number = %s,
                        country     = %s,
                        school      = %s
                    WHERE player_id = %s
                """, (
                    birthdate,
                    _safe_str(row.get("HEIGHT")),
                    _safe_str(row.get("WEIGHT")),
                    _safe_str(row.get("POSITION")),
                    _safe_str(row.get("JERSEY")),
                    _safe_int(row.get("DRAFT_YEAR")),
                    _safe_int(row.get("DRAFT_ROUND")),
                    _safe_int(row.get("DRAFT_NUMBER")),
                    _safe_str(row.get("COUNTRY")),
                    _safe_str(row.get("SCHOOL")),
                    pid,
                ))
                conn.commit()
            count += 1
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{total} bios synced ...", flush=True)
        except Exception as e:
            print(f"  Warning: failed to fetch bio for player_id={pid}: {e}")
            continue

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync NBA teams, players, and bios.")
    parser.add_argument("--bios", action="store_true", help="Also sync biographical data for active players")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "Set DATABASE_URL (e.g. postgresql://user:password@localhost:5432/nba). "
            "You can put it in a .env file."
        )

    conn = psycopg2.connect(database_url)
    ensure_schema(conn)

    print("Syncing teams ...", end=" ", flush=True)
    n_teams = sync_teams(conn)
    print(f"{n_teams} teams.")

    print("Syncing players ...", end=" ", flush=True)
    n_players = sync_players(conn)
    print(f"{n_players} players.")

    if args.bios:
        print("Syncing player bios (active players only) ...", flush=True)
        n_bios = sync_player_bios(conn)
        print(f"{n_bios} bios synced.")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
