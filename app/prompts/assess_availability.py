ASSESS_AVAILABILITY_PROMPT = """You are checking whether NBA players are available to play, based only on recent news snippets.

For each player below, decide their availability.

{players}

Return ONLY a JSON object keyed by the exact player name as written above, no markdown fences:

{{
  "Player Name": {{"status": "out" | "questionable" | "available", "note": "short reason, max 15 words"}}
}}

Rules — read these carefully, a wrong "out" removes a real pick from the board:

- "out" — the snippets clearly state the player is ruled out, done for the season, suspended, retired, or has undergone surgery with a recovery timeline that covers upcoming games.
- "questionable" — genuine doubt: day-to-day, a minutes restriction, listed as questionable/probable, returning from injury without a confirmed date, or in a contract/trade situation that has them not playing.
- "available" — everything else. This is the default.

- Judge ONLY from the snippets provided. Never use outside knowledge about a player's condition.
- Trade and signing news alone does NOT make a player unavailable — a player who changed teams still plays. Only mark them down if the snippets say they are not playing.
- Old or vague injury references ("has battled injuries", "injury-plagued past seasons") are NOT current absences. Mark "available".
- If the snippets are about a different player who happens to be mentioned alongside them, mark "available".
- The note must quote the actual reason from the snippets. If you cannot name a specific reason, the status is "available" and the note is "".
- Include an entry for every player listed above.
"""
