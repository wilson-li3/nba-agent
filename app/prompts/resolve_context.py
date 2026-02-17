RESOLVE_CONTEXT_PROMPT = """\
Given this conversation history and the user's new question, rewrite the question \
so it is fully self-contained (no pronouns like "he/she/they/his/their" that \
refer to earlier messages). If the question is already clear, return it unchanged.

Conversation:
{history}

New question: {question}

Output ONLY the rewritten question."""
