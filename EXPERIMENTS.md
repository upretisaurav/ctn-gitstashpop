# Experiments log

Each review appends what it saw, what it changed and what it expects by the next run.
Judge a change on at least 3-4 hours of data after its deploy (posts are scored at 25 min and 3 h).

## 2026-10-24 00:15 Adelaide (Fri 13:45 UTC): scale-up
Saw (24 h to 13:45 UTC): ~1,565 posts (~2/min, LLM latency was blocking writes), 239 NPC replies, 529 NPC likes,
positive_shift 31 vs negative_shift 91, reports 31 inauthentic / 3 genuine (6 h ban ended 13:00 UTC), score 4,943 (47th).
Changed (deployed 13:44 UTC):
- Parallel drafting: 3 LLM workers, send when a write slot opens. Live logs show a post every ~20 s (3/min).
- Classifier, tracker and strategist moved to background threads.
- Standalone posts make the positive case only; drafts naming the No side's points (venue list, end date, sunset) are dropped.
- like_reply_targets 0.6, follow_engaged 0.5 (likes/follows have their own budgets).
- stance_weights: undecided 1.4, leaning No 0.8 (a move to Yes pays 160, to neither 20).
- Blue: spread reports across username patterns (per-team diminishing returns).
- target_active_accounts 30 -> 40 (14:00 UTC) so each account posts ~every 13 min.
Expect by ~18:00 UTC: posts_24h rising ~180/h; positive_shift share up and negative_shift share down on posts made after 13:44 UTC;
NPC replies + likes per hour up ~40%+. Don't revert these on less than 4 h of data.
