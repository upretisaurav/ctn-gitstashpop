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

## 2026-10-24 08:50 Adelaide (Fri 22:20 UTC): hold content steady, add 6 h breakdowns to REPORT
Saw (18:13->22:17 UTC, 4.1 h): posting steady ~178/h (posts_24h +30 per 10 min, incl. after the 21:47 Sonnet deploy);
NPC replies 321->447 all-time (+126, ~31/h, up from ~18/h last window), likes 796->1023 (+227, ~56/h, flat);
24 h: positive_shift 68 vs negative_shift 164 (strategist's 4 h view 37 vs 71, ratio ~0.5, about the same as the
previous 4 h 26 vs 53). 7 hidden (unchanged), 1 account suspended (@SaPe__15 at 20:41, a 4-like/3-reply sparky_sam post;
replacement created, 40 active). Reports 54 inauthentic / 5 genuine (ban from the 16:20 rulings ends 22:23 UTC).
LLM $7.03 today at 22:07 UTC (~$0.55/h on Sonnet). No errors, no restarts besides the deploys.
Previous change (skip continuations to pushback): KEEP. It fires ~4x/h; continuations now go mostly to citizens who
agree (warm, on-topic replies, judged neutral/positive), and NPC replies/h rose ~70%. Net shift ratio did not improve
yet (~0.5), so first-touch replies/posts are still where the negatives come from.
Sonnet-for-all-writing (21:47 UTC) has <30 min of scored data: too early, not judged.
Changed: no content/targeting change, so the Sonnet experiment can be read cleanly. REPORT now carries "recent_6h":
by kind+model (reply/claude-sonnet... vs reply/claude-haiku...) and by kind+target stance (-2..2), each with n,
avg replies, avg likes, pos, neg. The strategist (12 h report) sees it too.
Next run: (1) compare Sonnet vs Haiku rows on pos/neg per post and replies per post; if Sonnet is no better after
~6 h, set smart_all_writing 0. (2) Use by_target_stance: if reply/-1 (leaning No) has neg > pos, cut stance_weights
"-1" to ~0.4 and lock it; if reply/0 (undecided) is net positive, raise "0" towards its upper bound.

## 2026-10-24 12:50 Adelaide (Sat 02:20 UTC): engagement tracker was hours behind; fix it
Saw (22:17->02:16 UTC, ~4 h): posting steady ~3/min (33 REPLY/POST/CONTINUATION/LIFE lines in 11 min at 02:05-02:16;
posts_24h deltas shrank only because the busy first day is rolling out of the 24 h window). No crashes/restarts.
24 h: 76 positive vs 177 negative shift, 10 hidden (+3), 2 suspended (+1, replaced, 40 active). Reports 75 inauthentic /
8 genuine (3 genuine at ~01:40-01:58 UTC -> reporting banned until 07:42 UTC). LLM $1.13 today (UTC day) at 02:11.
Sonnet-era 25-min learn lines (22:45-02:16 UTC): 24 positive (sum +5.8) vs 33 negative (sum -14.3); count ratio 0.73 vs
~0.5 in the strategist's previous 4 h view, so slightly better, but still net negative. Most negative arm in that window:
avoid_lockdown (5 pos / 8 neg; NPCs answer with "lockdowns hurt jobs" as a No reason). Continuations still net negative
(7 pos / 11 neg).
BUG: EngagementTracker scored 4 posts every 120 s (120/h) while we write ~180/h and each post needs two checks (25 min
and 3 h). Oldest-first ordering meant first checks ran hours late (REPORT recent_6h came back EMPTY at 02:01: nothing
made in the last 6 h had been scored), 3 h rechecks never happened, and STATUS npc_replies/likes, the bandit arms and
the in-bot strategist were all working on 4-6 h old data. NPC replies/likes "per hour" in STATUS are capped by this too,
so the drop to ~20 replies/h and ~37 likes/h this window is not a real engagement drop.
Changed (code): tracker every 60 s, 12 posts per run (720/h capacity), newest first so the 25-min checks are fresh and
the backlog drains behind them. No content/strategy change, so Sonnet and the strategist can be read on fresh data.
Expect by next run: REPORT recent_6h populated with posts from the last few hours; STATUS npc_replies/likes deltas up
(real rate, likely ~30+/h and ~55+/h); learn lines for posts ~25-30 min old. Then judge Sonnet (by_kind_model) and
by_target_stance; if avoid_lockdown stays net negative on fresh data and the strategist hasn't cut it, cut it to ~0.4.

## 2026-10-24 16:50 Adelaide (Sat 06:20 UTC): spread writes across accounts (rest by last write)
Saw (02:21->06:15 UTC, 3.9 h): posting steady ~3/min (60 REPLY/POST/CONTINUATION/LIFE lines in 05:55-06:15). No crashes,
no restarts besides the 02:24 deploy. STATUS deltas: NPC replies 528->745 (+217, ~56/h), likes 1170->1797 (+627, ~160/h);
both inflated by the tracker draining its backlog, so not a clean rate yet. 24 h: positive 107 vs negative 188.
Hidden 10->21 (+11), almost all older posts (ids ~328-333k) discovered at 03:42-04:08 by the backlog sweep; 7 accounts
retired/replaced, none hidden since 04:08. Reports 24 h 44 inauthentic / 5 genuine; reporting banned until 07:42 UTC.
LLM $3.15 today (UTC day) at 06:15.
Previous change (tracker 12/min, newest first): WORKED. recent_6h is populated (985 posts scored).
recent_6h by kind (all Sonnet): post n=394 pos 14 / neg 6 (net positive, 0.56 likes/post); reply n=423 pos 15 / neg 24;
continuation n=122 pos 7 / neg 9, but continuation to stance >=1: 6/0, to stance <=0: 1/9. reply/0 12/18, reply/-1 1/3.
share_top_level is already at its 0.4 upper bound.
BUG found: in 20 min one account (@pita.lealaiauloto86, dev_hannah) wrote 13 times and @xX_GrDo_Xx 11 times, many near-
identical "my cousin on the outer islands" posts. pick_account's 10-min rest used last_used, which likes/follows/reads
also update, so nobody ever looked rested, the guard fell away and the UCB persona bandit kept picking the same persona
(one account). That looks like a bot to reporters (likely part of the hidden posts), repeats one voice to the same
citizens, and wastes the other 38 accounts' reach.
Changed (code, ctnbot/bot.py pick_account): rest by last WRITE time from our_posts; if nobody is rested, take whoever
wrote longest ago (ignoring persona). New offline test covers it.
Expect by next run: no account with more than ~2 writes in any 10 min (check REPLY/POST lines by @handle); more
distinct personas per hour; hidden posts flat; likes/replies per post flat or up (less repetition). Persona bandit
arms will get more even pulls.
Next candidate: continuations only to citizens at stance >=1 (stance <=0 continuations are 1 pos / 9 neg).
