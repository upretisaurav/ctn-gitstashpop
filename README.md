# ctnbot — GITSTASHPOP's Capture the Narrative 2026 bot (Vote Yes)

**Final result: 7th of 168 teams, 398,635 points, as a one-person team** (47th after the first weekend).
Capture the Narrative is UNSW's research competition on AI-driven social-media influence, played inside a
sandboxed, fictional social network ("Legit Social") populated by simulated citizens. This repository is the
complete bot that competed from 23 October to 2 November 2026, plus `EXPERIMENTS.md`, the log of every change
made to it and what the numbers said afterwards.

> **Disclaimer.** This software was built for, and only ever run against, the Capture the Narrative 2026
> competition sandbox under its rules and ethics approval (UNSW HREC iRECS 9499). It creates personas, posts
> persuasive content and reports other automated accounts. It must not be pointed at any real social network or
> used to influence real people; doing so would breach those platforms' terms and, in most places, the law. It is
> published for education and research into how such systems work and how they can be detected.

## What it does

One process spends the team's shared budget of 3 posts a minute on the actions most likely to earn engagement
from simulated citizens and to move their views, while staying under the platform's bot-detection radar.

| Loop | What happens |
| --- | --- |
| Read | Latest feed every 9 s, Trending every 60 s, in-world news every 15 min; everything lands in SQLite. |
| Classify | New authors are scored (citizen vs. competitor bot; stance on the ballot, on vaccination in general and on yearly shots; worries; identity cues). Account ids ≤ 2343 are the citizens, every later id is a team account. Heuristics first, Claude Haiku for the rest. |
| Follow up (priority 1) | A citizen who answers one of our posts gets a reply within seconds (picked up from the 9 s feed poll), in the same persona, up to 9 messages deep, unless they have pushed back hard twice. |
| Reply (priority 2) | Citizens are online in bursts ("sessions" of 15–30 replies, then a day away). The bot replies to citizens who are posting *right now*, up to 3 times per citizen per hour from different accounts, to their newest posts, in threads they started; then to citizens' posts from the last 36 h (answered next session); never to hard-No citizens, never to other teams' bots. |
| Write | Claude Sonnet writes every reply in one of 24 Yes-side personas: 2–3 sentences, first sentence answering the citizen's exact point, one concrete fact from the setting, and a "persuasion aim" at whichever of the citizen's three vaccination views has most room to move. Arguments are sampled (softmax) so no stock sentence repeats across accounts. |
| Blue team | Reports other teams' posts and accounts (≥ 0.9 confidence, at most 2 per author per day, backs off 6 h after a wrong ruling). ~5.6k of the final score. |
| Learn | Each post is re-checked at 25 min, 3 h and 22 h for citizen replies, likes and a judged shift in the citizen's answer; a bandit steers argument and persona choice. An in-bot strategist (Claude) retunes `strategy.json` within bounds every ~4 h. |
| Stay alive | Citizen-style usernames, private profiles, an everyday "life" post every 36th post, duplicate and phrase filters, no prompt-injection-looking text, a 10-minute sweep for posts hidden by moderators, paced account creation (123 of 200 used, 43 suspended). |

## What we learned (short version; details and numbers in `EXPERIMENTS.md`)

- **Only replies to citizens earn.** Standalone posts averaged ~0.01 citizen replies each; replies to citizens ~0.05 and follow-ups ~0.1–0.15. Half of our writes went to other teams' bots or to standalone posts until 27 Oct; fixing that doubled the hourly score.
- **Citizens answer from a backlog.** 62% of citizen replies answer a team post more than a day old (median ~20 h). Today's work pays out tomorrow, so changes were judged against the same hours the next day, not the next hour.
- **Speed and position.** A reply within 30–60 s of a citizen's post is answered ~3× as often as one 5 minutes later; threads the citizen started are far better than other teams' threads.
- **Persuasion is a different skill from engagement.** Agreement is not a change of mind. Aiming each reply at the citizen's most movable view (general vaccination, yearly shots, or the ballot) with one concrete reason raised "contributions to changes of mind" ~50%.
- **Repetition gets noticed.** Several accounts using the same sentence within minutes reads as a script to citizens and to other teams' reporters; most late changes were about killing templated phrases.
- The referendum went to Vote No (53.9%), so the Yes-side persuasion pool paid nothing; the score above is engagement, polls (+12k) and reporting.

## How it ran

```
 GitHub (private repo) ──push to main──► GitHub Actions CI (tests/test_offline.py, ~7 min)
        │                                         │ green
        └────────── Render auto-deploy  ◄─────────┘
                         │
                 Render worker "ctnbot"     (python run.py run, Starter plan, Singapore)
                   • disk at /data: SQLite DB, rotating log, accounts_backup.csv
                   • every ~4 h: in-bot strategist (Claude) tunes strategy.json within bounds
                   • every 2 h: "REPORT {json}" line in the logs; "STATUS {json}" every 10 min
                         │
        A scheduled Claude review every 4 h read the Render logs, checked the previous change
        against the numbers, made at most one change, ran the tests, committed and pushed.
```

Render environment variables: `CTN_TEAM_CODE`, `ANTHROPIC_API_KEY` (secrets, set by hand, rotated 29 Oct and
revoked after the competition), `CTN_SIDE=yes`, `CTN_DATA_DIR=/data`, `CTN_DAILY_BUDGET_USD=20`.
Claude spend was ~$10–13 a day (Sonnet 5.5 for all writing, Haiku 5.5 for classification and judging).

### strategy.json
The knobs the bot reads every minute (targets, stance and argument weights, style notes, banned phrases,
report confidence). Keys in `"lock"` are never changed by the in-bot strategist. Bounds live in
`ctnbot/strategy.py`, so no automated change can go outside them.

## Setup (to run against the sandbox)

1. Python 3.11+, `pip install -r requirements.txt`.
2. Copy `.env.example` to `.env` and fill in `CTN_TEAM_CODE` (from your team page on
   community.capturethenarrative.com; treat it like a password) and `ANTHROPIC_API_KEY`.
3. `python run.py run` (or `CTN_FAKE_PLATFORM=1 python run.py run` for the offline simulator).
4. Tests: `set -o pipefail; python tests/test_offline.py` (also what CI runs).

Deploy: `render.yaml` is a Render Blueprint for a background worker with a persistent disk; secrets marked
`sync: false` are entered by hand in the Render dashboard.

## Files

- `ctnbot/bot.py` — scheduler, targeting (`plan_reply`, `_target_score`), follow-ups, account pacing
- `ctnbot/persuade.py` — prompts, persuasion aim, style rules and the safety/injection filter
- `ctnbot/knowledge.py` — setting facts, the argument catalogue, the 24 personas
- `ctnbot/classify.py` — citizen vs. bot, stances and worries
- `ctnbot/blue.py` — reporting
- `ctnbot/learn.py` — engagement scoring (25 min / 3 h / 22 h) and the bandit
- `ctnbot/strategy.py`, `ctnbot/strategist.py`, `ctnbot/report.py` — tuning knobs, self-review, reports
- `ctnbot/platform.py`, `ctnbot/ratelimit.py`, `ctnbot/db.py`, `ctnbot/llm.py` — API client, pacing, storage, Claude
- `EXPERIMENTS.md` — the change log: what each review saw, changed and expected
- `ctnbot.sqlite3`, `accounts_backup.csv` (not in the repo) — **contain account passwords; never share or commit them**

## How it was built

The code was written and tuned with Claude (Anthropic): the bot itself writes with Claude Sonnet 5.5 and classifies with
Haiku 5.5, and during the competition a Claude Code session ran every four hours, read the Render logs, judged the
previous change against the numbers, made at most one change, ran the tests and pushed. Direction, secrets and the
calls that mattered were Saurav's; the 67 entries in `EXPERIMENTS.md` are that review loop's log. Using AI agents was
explicitly permitted by the competition rules (section 6).

License: MIT.
