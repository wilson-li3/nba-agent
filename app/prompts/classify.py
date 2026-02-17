CLASSIFY_PROMPT = """Classify the following NBA-related question into one of five categories.

## Categories and Examples

**STATS** — Player/team statistics, records, game scores, standings, comparisons, historical data.
Examples:
- "Who scored the most points last season?"
- "What is LeBron's career PPG?"
- "How many triple-doubles does Jokic have?"
- "What are the current NBA standings?"
- "How is Jayson Tatum playing this season?"
- "Who are the top 10 scorers this year?"
- "What's the Warriors' record this season?"
- "Compare Steph Curry and Damian Lillard's three-point shooting"

**NEWS** — Recent events, trades, injuries, rumors, coaching changes, free agency, front office moves.
Examples:
- "What are the latest trade rumors?"
- "Is Kawhi Leonard injured?"
- "Who got traded today?"
- "What happened with the coaching change in Phoenix?"
- "Any updates on Zion's injury status?"
- "What are the latest free agency signings?"
- "Is there any news about the Lakers front office?"
- "What draft picks did the Celtics acquire?"

**MIXED** — Questions that need BOTH statistical data AND recent news/event context to answer properly.
Examples:
- "How has the new trade impacted the team's win percentage?"
- "Compare the stats of players involved in the latest trade."
- "How has James Harden been playing since the trade?"
- "What's the team's record since the coaching change?"
- "How are the Lakers performing after the Anthony Davis injury?"
- "Has Donovan Mitchell's production changed since the trade deadline?"
- "How did the roster shakeup affect the bench scoring?"
- "What's Tyrese Maxey's usage rate since Embiid went down?"

**BETTING** — Sports betting analysis: prop bets, parlays, value picks, over/unders, betting recommendations.
Examples:
- "Should I take Tatum over 25.5 points tonight?"
- "What are the best player props today?"
- "Analyze this parlay: Jokic over 10 rebounds, Curry over 3 threes"
- "Who's been smashing their props lately?"
- "Any value picks for tonight's games?"
- "Give me SGP legs for the Lakers game"
- "Is the over good for the Celtics game?"
- "Should I fade Westbrook tonight?"

**OFF_TOPIC** — Questions not related to the NBA.
Examples:
- "What is the capital of France?"
- "Write me a poem"
- "Who won the Super Bowl?"
- "How do I cook pasta?"
- "What's the weather today?"
- "Who won the World Series?"
- "Tell me about soccer transfers"
- "What's happening in the NFL draft?"

## Decision Rules (apply in order)

1. If the question mentions "bet", "prop", "parlay", "over/under", "pick", "SGP", "same game parlay", "fade", "hammer", or "smash" in a betting context → BETTING
2. "Should I take..." with a stat line → BETTING
3. If unsure between STATS and BETTING but any betting language is present → BETTING
4. If the question mentions BOTH a trade/injury/event AND performance/stats → MIXED
5. Phrases like "since the trade", "after the injury", "since the coaching change" → strongly signals MIXED
6. "How is X playing" with no event reference → STATS
7. Team records, standings, win-loss → STATS
8. Trade rumors, injury reports, front office news, draft news → NEWS
9. Non-NBA sports or non-sports topics → OFF_TOPIC
10. When unsure between STATS and MIXED → choose STATS
11. When unsure between NEWS and MIXED → choose MIXED

Respond with exactly one word: STATS, NEWS, MIXED, BETTING, or OFF_TOPIC.

Question: {question}
"""
