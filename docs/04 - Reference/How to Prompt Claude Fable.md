# How to Prompt Claude Fable
**Status:** Living Document · **Date:** 2026-07-29 · **Applies to:** working with Fable-5 (external reference; not project-specific)

HOW TO PROMPT CLAUDE FABLE-5:
(for people who don't code)

1. Your Chat Length Is Your $$$ Bill:
. Fable re-reads the entire thread on every turn. Long chats = big bills.
. One goal per chat. Give only two turns max.
. Fresh chat, Fable 5 (High), go.

2. Give It Goals. Never Tasks.
. Fable plans, asks, pushes back, checks its own work.
X Task: "Write a follow-up email to this client."
V Goal: "This client owes me 2 invoices & went quiet 3 weeks ago. Goal:
get paid without burning the relationship. Plan your approach & ask me
what you don't know."
3. Steal This Prompt Template:
. "I need [task] for [goal]. I expect [goal] achieved once we hit [specific
targets]. Start by asking me questions to fully understand the context."
. Fable asks. You answer. Then it thinks deeply. Don't skip the questions.

4. Kill The Buried Answer:
. Add: "Lead with the outcome. Your first sentence answers the question.
Details after."
. No more scrolling to find the answer.

5. Add The Anti-BS Line:
. Add: "Only report work you have evidence for. If it's not verified, say so."
. Kills fake "all done!" reports.

6. Say Which Mode You're In:
. Feedback? "Give me your thoughts. Don't write anything yet."
. Delegating? - "Handle end-to-end. Only check if something is irreversible."
. Deciding? "Act, when you have enough info."

7. Exit Before It Gets Expensive:
. After 2 turns, switch to Opus 4.8 (High). Same chat, keeps context.
. Fable sets the strategy. Opus does the grunt work. Bill stays at $O.