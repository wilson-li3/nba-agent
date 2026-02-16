CLASSIFY_PROMPT = """Classify the following NBA-related question into one of three categories:

- STATS: Questions about player statistics, records, game scores, standings, comparisons, historical data.
  Examples: "Who scored the most points last season?", "What is LeBron's career PPG?", "How many triple-doubles does Jokic have?"

- NEWS: Questions about recent events, trades, injuries, rumors, coaching changes, free agency.
  Examples: "What are the latest trade rumors?", "Is Kawhi Leonard injured?", "Who got traded today?"

- MIXED: Questions that need both stats and recent news context.
  Examples: "How has the new trade impacted the team's win percentage?", "Compare the stats of players involved in the latest trade."

- OFF_TOPIC: Questions that are not related to the NBA at all.
  Examples: "What is the capital of France?", "Write me a poem", "How do I cook pasta?", "What's the weather today?"

Respond with exactly one word: STATS, NEWS, MIXED, or OFF_TOPIC.

Question: {question}
"""
