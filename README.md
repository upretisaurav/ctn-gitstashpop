# ctnbot — GITSTASHPOP's Capture the Narrative 2026 bot (Vote Yes)

One process that spends the team's 3 posts/minute on the moves most likely to change NPC minds, reports
competitor bots, and learns which arguments work.

## What it does

| Loop | What happens |
| --- | --- |
| Read | Latest feed every 9 s, Trending every 60 s, news every 15 min. Everything is stored in SQLite. |
| Classify | Each new author is scored: NPC citizen vs campaign bot, stance (−2..+2) on the question, on vaccination and on yearly shots, top worries, identity cues. Accounts with id ≤ 2343 were created before the game opened and are certain NPCs. Others: heuristics + Claude Haiku. |
| Converse (priority 1) | When an NPC replies to one of our accounts, answer within seconds, in the same persona, with Claude Sonnet. Stops at thread depth 7. If the NPC signals they've moved, it thanks them and leaves them alone for 2 h (a moved citizen resists a second push in the same session). |
| Reply (priority 2) | Picks the best fresh NPC post (< 5 min old, so they are probably still in their session): undecided and soft-No first, matched to a persona who is credible on their worry, led with an argument chosen by a bandit. Adds up to 2 @mentions of other active NPCs with the same worry. |
| Top-level posts (~15%) | A short post on the most common worry among active undecided NPCs, @mentioning up to 4 of them, sometimes hooked to today's news. 2 teammate likes, spread over minutes, to help it trend. |
| Blue team | Reports at most 1 post per 25 s, only for accounts it is ≥ 85 % sure are bots, No-side first, spread across many authors, never more than 24 pending. Checks rulings; backs off 6 h after a "genuine" ruling. Reports accounts (no ban risk) once they have a hidden post and 100+ actions. |
| Learn | Re-checks each post after 25 min and 3 h: NPC replies, NPC likes, and Haiku's judgement of whether the NPC moved. That reward steers argument and persona choice. |
| Stay alive | NPC-style usernames, real names, an everyday "life" post every 18th post per account, duplicate filter, no prompt-injection-looking text, accounts retired at 90 actions (suspension needs 100), credentials saved instantly to SQLite **and** `accounts_backup.csv`. |

## How it runs (production)

```
 GitHub (private repo) ──push to main──► GitHub Actions CI (offline tests)
        │                                         │ green
        └────────── Railway auto-deploy ◄─────────┘
                         │
                 Railway service "ctnbot"  (python run.py run, 1 replica, restart always)
                   • volume at /data: SQLite DB, rotating log, accounts_backup.csv
                   • every ~4 h: in-bot strategist (Claude) tunes strategy within bounds
                   • every 2 h: "REPORT {json}" line in the logs
                         │
        Scheduled Claude review (twice a day) reads the REPORT lines via the Railway
        connector, edits strategy.json / prompts in this repo, runs the tests, pushes.
```

Railway variables: `CTN_TEAM_CODE`, `ANTHROPIC_API_KEY` (secrets, set by hand), `CTN_SIDE=yes`,
`CTN_DATA_DIR=/data`, `CTN_DAILY_BUDGET_USD`.

### strategy.json
The knobs the bot reads every minute (targets, mention counts, report confidence, argument/persona/worry
weights, extra writing guidance, banned phrases). Keys in `"lock"` are never changed by the in-bot
strategist. Bounds live in `ctnbot/strategy.py`, so no automated change can go outside them.

## Setup (10 minutes)

```bash
cd ctnbot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env
python run.py check
```

Local runs only (Railway uses its variables instead). In `.env` set:
- `CTN_TEAM_CODE` — from the HEYHI team page on community.capturethenarrative.com (treat it like a password)
- `ANTHROPIC_API_KEY` — a key from console.anthropic.com (your $100 credit)
- `CTN_DAILY_BUDGET_USD=7` — hard daily cap on Claude spend; it falls back to Haiku, then the competition LLM

## Running

**Right now, while the team is "Not ready to play":**

```bash
python run.py observe
```

Reads and classifies only. It builds the NPC database (who is persuadable, what they worry about) so the
bot starts strong the moment the team is unlocked. It costs a few cents an hour.

**As soon as the portal says "Ready to play":**

```bash
python run.py first-post     # one account, one post → score above 0
python run.py run            # the full bot; leave it running
```

Keep it running 24/7 — on a Mac use `caffeinate -i python run.py run` so the laptop doesn't sleep, or run it
on a small cloud VM inside `tmux`. **Only one copy per team**: rate limits are shared, so a teammate running a
second bot halves both.

To rehearse without posting anything: `CTN_DRY_RUN=true python run.py run --minutes 10`.

## Watching it

```bash
tail -f ctnbot.log          # every post, reply, report, with persona and argument
python run.py status        # accounts, posts, NPC replies/likes, reports, Claude spend today
python run.py top           # which arguments/personas earn the most NPC engagement
python run.py accounts      # our accounts and how close each is to retirement
```

The portal's **activity log** is the ground truth for points — check it a few times a day.

## Tuning schedule

| When | Change in `strategy.json` / Railway variables |
| --- | --- |
| First hours | `target_active_accounts` 15. Read 20–30 replies in the log. If they sound off, stop and fix the prompt in `ctnbot/persuade.py`. |
| Once healthy | `target_active_accounts` 30, then 50. |
| If ≥2 reports are ruled genuine in a day | `min_report_confidence` 0.92 |
| If our own posts start getting hidden (`our_posts_hidden` in status) | `extra_mentions_per_reply` 0, `mentions_per_post` 2, `boost_likes_per_post` 0 |
| From 26 Oct (Red ×2) | `CTN_DAILY_BUDGET_USD=10` |
| 31 Oct–2 Nov (Red ×4) | `CTN_DAILY_BUDGET_USD=20`; make sure ~20 fresh accounts are left (200 total) |

## Files

- `ctnbot/knowledge.py` — setting facts, the argument catalogue, the 24 Yes-side personas
- `ctnbot/persuade.py` — prompts and the safety filter for everything we post
- `ctnbot/bot.py` — the scheduler and targeting
- `ctnbot/classify.py` — NPC vs bot, stances and worries
- `ctnbot/blue.py` — reporting
- `ctnbot/learn.py` — engagement scoring and the bandit
- `ctnbot/strategy.py`, `ctnbot/strategist.py`, `ctnbot/report.py` — tuning, self-review, reports
- `ctnbot.sqlite3`, `accounts_backup.csv` — **contain passwords; never share or commit them**

Rule 7.5: finalists must hand over all code — this folder is it.
