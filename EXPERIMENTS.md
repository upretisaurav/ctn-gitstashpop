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

## 2026-10-24 00:50 Adelaide (Fri 14:20 UTC): stop validating the No case in replies
Saw (scale-up live since 13:44/14:09 UTC, under 1 h of data, so too early to judge it): posting every ~20 s (3/min, ~170/h);
10-min STATUS delta 14:10->14:20: +28 posts, +5 NPC replies (~30/h vs ~18/h avg earlier today), +9 likes (~54/h vs ~41/h).
24 h: 1,656 posts, 251 replies, 562 likes, positive_shift 33 vs negative_shift 94, 7 hidden, 0 suspended,
reports 31 inauthentic / 3 genuine (26 pending), LLM $4.67 today.
Diagnosis: concede_and_pivot is our most-used arm (444 pulls) and its brief told the writer to "agree honestly with their
condition (list, end date...)"; STYLE_RULES also said "concede what is true". Worst posts (and ~1 in 6 of the ~60 negative-shift
posts in the 09:00-14:20 log) are replies/continuations saying "the list isn't out yet, which bothers me too" / "I won't pretend
the details are nailed down", after which the citizen restates the No case. The scale-up style_notes only covered posts.
Changed (code, not strategy.json, so the strategist overlay can't undo it):
- STYLE_RULES: acknowledge the person, not the No argument; never repeat/validate the venue list, end date, "blank cheque",
  staff-checking points; no "bothers me too" / "won't pretend".
- concede_and_pivot gist rewritten to "acknowledge briefly without repeating, details go in the regulations, spend the reply on Yes".
- Continuation prompt: answer a new doubt with a reason for Yes without restating it.
Expect by ~18:30 UTC (posts after ~14:35 UTC): negative-shift share of scored posts down (aim positive >= 0.6x negative on
new posts, from 0.35x); reply/like rates unchanged or slightly lower. If replies per post fall >30% with no shift gain, soften.

## 2026-10-24 04:50 Adelaide (Fri 18:20 UTC): stop continuing threads with citizens who push back
Saw (14:32->18:13 UTC, 3.7 h after the 14:31 deploy): posting steady ~3/min (STATUS posts_24h +29-30 per 10 min);
24 h rolling: 2,303 posts, 321 NPC replies (+66 in window), 796 likes (+217), 7 hidden (unchanged), 0 suspended,
positive_shift 48 vs negative_shift 116 (in-window +13 vs +18; strategist's 4 h view 26 vs 53). Reports 53 inauthentic /
5 genuine (2 new genuine at ~16:20 UTC -> reporting banned until 22:23 UTC). LLM $6.02 today. Bursts of API read
timeouts 14:40-16:16 UTC (platform side), no crashes or restarts.
Previous change (stop validating the No case): WORKING for first-touch posts/replies. Of 25-min scores with NPC replies
since 14:55 UTC, replies/posts with an argument: 23 positive vs 14 negative (was ~0.35x before). Worst-post list is all
pre-change. Keep it.
New finding: continuations (our second message in a thread) are now the main loss: 4 positive vs 11 negative
(sum -5.8 over 16), while being ~25% of output. Typical: citizen pushes back, we answer again, they restate No.
Changed (code, ctnbot/bot.py compose_continuation): before drafting a continuation, judge the citizen's answer with the
cheap Haiku judge; if shift <= -0.4 (pushing back) skip it, mark them last_targeted so plan_reply rests them, and use
the slot for a fresh first-touch reply. Continuations to people who are neutral or moving our way are unchanged.
Expect by ~22:30 UTC: continuation count down ~40-60%, first-touch replies up by the same; negative_shift growth per
hour down ~30%; positive >= negative in the 25-min learn lines. NPC replies/hour may dip slightly (continuations had the
best reply rate, 0.3/post). If replies/h fall >25% with no shift gain, raise CONTINUE_MIN_SHIFT to -0.7.

## 2026-10-24 08:20 Adelaide (Fri 21:50 UTC): Sonnet for all writing (manual, with Saurav)
The $100 promotional API credit expires 27 Oct and would otherwise go unused, so all replies and standalone posts now use
Sonnet (new knob smart_all_writing = 1.0, locked). Life posts, classification and judging stay on Haiku.
Estimated spend ~$15-18/day at 3 writes/min (daily cap CTN_DAILY_BUDGET_USD = 20; smart calls fall back to Haiku when
under $0.50 is left). Saurav tops up paid credit on Mon 26 Oct.
Expect: likes/replies per post and positive-vs-negative shift on Haiku-written posts (POST lines were Haiku) to improve.
Compare by model in the REPLY/POST log lines. If shift and engagement per post don't improve within ~6 h, set it back to 0.
