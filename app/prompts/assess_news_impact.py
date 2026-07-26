ASSESS_NEWS_IMPACT_PROMPT = """You are a betting analyst reading recent NBA news to judge whether it changes the outlook for one specific player prop.

Player: {player}
Prop: over {line} {stat}

Recent news excerpts (may be unrelated — judge relevance yourself):
{chunks}

Return ONLY a JSON object, no markdown fences, with these fields:

- "impact": one of "out", "negative", "neutral", "positive"
  - "out": the player is ruled out, suspended, or clearly not playing
  - "negative": injury doubt, minutes restriction, a returning teammate taking usage, illness
  - "positive": a teammate is out (usage bump), returning from injury into a bigger role, recent role/minutes increase
  - "neutral": nothing in the excerpts materially changes this player's expected production
- "multiplier": a number between 0.0 and 1.15 applied to the player's projected mean
  - 0.0 only when impact is "out"
  - 0.88-0.97 for negative, 1.0 for neutral, 1.03-1.12 for positive
  - Be conservative. Most news is noise; use 1.0 unless the excerpts are specific and recent.
- "note": one short sentence (max 20 words) explaining the call in plain language. If neutral, say what you checked.
- "confidence": "low", "medium", or "high" — how directly the excerpts speak to this player's next game

Rules:
- Only react to news about THIS player or their direct teammates. Ignore league-wide or unrelated stories.
- If the excerpts do not mention this player or team at all, return impact "neutral", multiplier 1.0, confidence "low".
- Never invent facts that are not in the excerpts.
"""
