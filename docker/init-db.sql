-- Extensions the app depends on. pgvector powers news search; unaccent is
-- used by every fuzzy player-name lookup (unaccent(display_name) ILIKE ...),
-- so a database without it fails at query time, not at startup.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;
