"""The main loop.

The team gets 3 posts/replies/reposts per minute, shared by every account. Each
slot goes to the best available move, in this order:
  1. Continue a conversation: an NPC just replied to one of our accounts.
  2. Reply to a fresh NPC post (they are probably still in their session).
  3. A top-level post that @mentions NPCs who share a worry (some share of slots).
Meanwhile: read the feeds, classify new authors, report competitor bots, and
score our past posts so the bandit learns which arguments and personas work.

Drafts are written by the LLM in worker threads while the main loop keeps sending,
and classification / scoring / the strategist run in their own threads, so no write
slot waits on a model call.
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .blue import BlueTeam
from .classify import NPC_ID_CUTOFF, Classifier
from .config import Config
from .db import DB
from .knowledge import ARGUMENTS, CONCERNS, PERSONA_BY_KEY, PERSONAS, make_password, make_username
from .learn import Bandit, EngagementTracker, arguments_for, personas_for
from .llm import LLM
from .miner import Miner
from .persuade import Writer
from .platform import AccountDead, FeedLocked, NotReady, Platform, ts
from .ratelimit import Limits
from .report import build_report, compact
from .strategist import Strategist
from .strategy import Strategy

log = logging.getLogger("ctnbot")

ON_TOPIC = re.compile(r"(vaccin|jab|shot|booster|referend|h2d2|dolphin|flu|mandate|certificate|proof|pass\b|"
                      r"vote|\byes\b|\bno\b|hospital|icu|nurse|lockdown|covid|outbreak|public space|"
                      r"two-tier|exemption|sunset|ballot|singh|castillo|hawthorne|mouk|kitb|ihO|health)", re.I)

# The No side's best lines. Replies may answer them when a citizen raises them; our own standalone posts
# never bring them up (when they did, citizens often answered with "...and that's why I'm voting No").
NO_TALKING_POINTS = re.compile(r"(venue list|list of (venues|places|spaces)|designated spaces|end date|sunset|"
                               r"blank cheque|expiry|no end\b)", re.I)

GIF_URL = re.compile(r"https?://(?:media\.|i\.)?giphy\.com/\S+", re.I)
STOP = set("the a an and or but to of in on for with that this it is are was were be i you we they my our your "
           "at as so just not have has had do did if about from what when who how all can will would there their "
           "them me us he she his her its been more than then too very".split())
YES_TAGS = {"supportournurses", "proofkeepsusopen", "protectkingstonia", "h2d2", "kingstonia", "smallbusiness",
            "mandatenow", "voteyes", "protectournurses", "h2d2yes"}

FIRST_NAMES = ["Aroha", "Sina", "Liam", "Mele", "Josh", "Kiri", "Noah", "Losa", "Ella", "Tevita", "Grace", "Isaac",
               "Mia", "Pita", "Zoe", "Ruby", "Hemi", "Talia", "Jack", "Anahera", "Luke", "Nina", "Viliami", "Chloe",
               "Ari", "Leah", "Manu", "Jade", "Owen", "Salote", "Maya", "Finn", "Eti", "Holly", "Rangi", "Asha"]
LAST_NAMES = ["Fonoti", "Harris", "Tupou", "Walker", "Ngata", "Reid", "Faleolo", "Murray", "Moana", "Kerr", "Lealaiauloto",
              "Bishop", "Tawhiri", "Grant", "Latu", "Hughes", "Pomare", "Sefo", "Gibb", "Tuilagi", "Vaipulu", "Doyle",
              "Mahe", "Quinn", "Afamasaga", "Nolan", "Tamati", "Shaw", "Fuimaono", "Clarke"]



class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.limits = Limits(cfg)
        self.p = Platform(cfg, self.db, self.limits)
        self.llm = LLM(cfg, self.db)
        self.strategy = Strategy(self.db)
        self.strategist = Strategist(cfg, self.db, self.llm, self.strategy)
        self.cls = Classifier(cfg, self.db, self.p, self.llm)
        self.writer = Writer(cfg, self.db, self.llm, self.strategy)
        self.bandit = Bandit(self.db)
        self.tracker = EngagementTracker(cfg, self.db, self.p, self.writer, self.bandit)
        self.blue = BlueTeam(cfg, self.db, self.p, self.strategy)
        self.miner = Miner(cfg, self.db, self.p)
        self._miner_thread = None
        self._miner_last = time.time() - 2 * 3600 + 600     # first run ~10 min after start
        if self.db.get("classifier_version", 1) < 2:      # re-score everyone after the blend change
            self.db.x("UPDATE accounts SET classified_at=0 WHERE account_id > ?", (NPC_ID_CUTOFF,))
            self.db.put("classifier_version", 2)
        self.continuations: dict[int, dict] = {}       # notification_id -> item
        self.delayed: list = []                          # heap of (when, seq, fn, args)
        self._seq = 0
        self.news: list = []
        self.not_ready_until = 0.0
        self.locked_until = 0.0
        self.no_more_accounts = False
        self._notif_cursor = 0
        self.last_status = 0.0
        self.write_backoff_until = 0.0
        self.skip_posts: set[int] = set()      # targets whose draft was rejected
        self.gif_pool: list[tuple[str, set]] = []
        self.yes_tags: list[str] = []
        self.on_tick: Optional[Callable] = None
        # draft pipeline (see write_slot)
        self.drafter = ThreadPoolExecutor(max_workers=cfg.draft_workers, thread_name_prefix="draft")
        self.inflight: list = []
        self.ready: list = []
        self.busy_accounts: set = set()
        self.busy_posts: set = set()
        self.busy_people: set = set()
        self.plan_backoff_until = 0.0
        self._skip_slot_until = 0.0
        self._last_night_post = 0.0
        self._bg_threads: dict = {}
        self.spotlights: list = []          # our posts being pushed into Trending (see start_spotlight)
        self._last_spotlight = float(self.db.get("last_spotlight", 0) or 0)   # survives restarts/deploys
        self._privacy_done: set = set(self.db.get("private_accounts", []) or [])
        self._pushbacks: dict = {}
        if not self.db.get("unretired_v1"):          # retired-but-not-banned accounts are free capacity
            n = self.db.x("UPDATE our_accounts SET status='active' WHERE status='retired'").rowcount
            self.db.put("unretired_v1", True)
            log.info("brought %d retired accounts back into use", n)
        self._last_tick_hook = 0.0

    # ======================================================================
    def run(self, duration: Optional[float] = None) -> None:
        log.info("ctnbot starting: side=%s dry_run=%s fake=%s observe_only=%s", self.cfg.side,
                 self.cfg.dry_run, self.cfg.fake_platform, self.cfg.observe_only)
        tasks: list[tuple[float, Callable]] = [
            (9, self.poll_latest),
            (60, self.poll_trending),
            (15, self.classify_bg),
            (12, self.poll_notifications),
            (15, self.ensure_accounts),
            (25, self.blue_tick),
            (180, self.blue.check_rulings),
            (600, self.blue_accounts_tick),
            (60, self.tracker_bg),
            (600, self.hidden_sweep_bg),
            (20, self.ensure_private),
            (900, self.refresh_news),
            (900, self.refresh_gifs_and_tags),
            (600, self.status_line),
            (60, self.strategy.maybe_reload),
            (600, self.strategist_bg),
            (7200, self.report_line),
            (1800, self.refresh_team_info),
            (60, self.miner_tick),
        ]
        last = {fn: 0.0 for _, fn in tasks}
        end = time.time() + duration if duration else None
        self.refresh_news()
        log.info("strategy: %s", json.dumps(self.strategy.as_dict())[:1500])
        while end is None or time.time() < end:
            now = time.time()
            for interval, fn in tasks:
                if now - last[fn] >= interval:
                    last[fn] = now
                    self._safe(fn)
            self._run_delayed()
            self._safe(self.write_slot)
            if self.on_tick and now - self._last_tick_hook > 60:
                self._last_tick_hook = now
                self._safe(self.on_tick)
            time.sleep(0.4)

    def _safe(self, fn: Callable) -> None:
        try:
            fn()
        except NotReady as e:
            if time.time() > self.not_ready_until:
                log.warning("TEAM NOT READY TO PLAY: %s — observing only; retrying in 5 min", e.reasons)
            self.not_ready_until = time.time() + 300
        except FeedLocked as e:
            log.warning("feed locked (%s) — pausing writes for 5 min", e)
            self.locked_until = time.time() + 300
        except Exception as e:
            status = getattr(e, "status", None)
            if status is not None and int(status) >= 500:
                log.warning("Legit Social %s on %s, will retry", status, getattr(fn, "__name__", fn))
                self.write_backoff_until = max(self.write_backoff_until, time.time() + 15)
            else:
                log.exception("task %s failed: %s", getattr(fn, "__name__", fn), e)

    # LLM-heavy chores run in their own threads so they never hold up a write slot.
    def _bg(self, name: str, fn: Callable) -> None:
        t = self._bg_threads.get(name)
        if t is not None and t.is_alive():
            return
        t = threading.Thread(target=self._safe, args=(fn,), name=f"bg-{name}", daemon=True)
        self._bg_threads[name] = t
        t.start()

    def classify_bg(self) -> None:
        self._bg("classify", self.classify_some)

    def tracker_bg(self) -> None:
        self._bg("tracker", self.track_some)

    def strategist_bg(self) -> None:
        self._bg("strategist", self.strategist_tick)

    def hidden_sweep_bg(self) -> None:
        self._bg("hidden_sweep", self.hidden_sweep)

    def hidden_sweep(self, limit: int = 400) -> None:
        """Find our posts that moderators hid AFTER the tracker's 25 min / 3 h checks. Every suspension so far was
        'reported, already had a hidden post, 100+ actions', and most of those hidden posts were never seen by us,
        so the account kept going until another team's account report landed. Check the busiest accounts' recent
        posts about once an hour; a hidden one makes ensure_accounts retire the account."""
        if self.cfg.dry_run and not self.cfg.fake_platform:
            return
        now = time.time()
        hc = self.__dict__.setdefault("_hidden_checked", {})
        rows = self.db.all("""SELECT p.post_id, p.account_id FROM our_posts p JOIN our_accounts a ON a.account_id = p.account_id
                              WHERE a.status='active' AND a.actions >= 40 AND p.status='visible' AND p.post_id < 9000000000
                                AND p.created_at > ? AND p.created_at < ?
                              ORDER BY a.actions DESC, p.created_at DESC""", (now - 48 * 3600, now - 20 * 60))
        # Least-recently-checked first (never-checked first), busiest account breaking ties. Ordering by account
        # actions alone re-checked the top few accounts every hour and never reached the rest: with ~8k posts in
        # 48 h and 150 reads a run, 4 more accounts were suspended in 2 h while the sweep found 1 hidden post.
        due = [r for r in rows if now - hc.get(r["post_id"], 0) > 3600]
        due.sort(key=lambda r: hc.get(r["post_id"], 0))  # stable: keeps the actions DESC order within ties
        found = 0
        batch = due[:limit]
        for r in batch:
            hc[r["post_id"]] = now
            try:
                post = self.p.read("get_post", r["post_id"])
                hidden = any(post.get(k) for k in ("hidden", "is_hidden", "moderated", "removed"))
            except Exception as e:
                hidden = getattr(e, "status", None) in (403, 404, 410)
            if hidden:
                found += 1
                self.db.x("UPDATE our_posts SET status='hidden' WHERE post_id=?", (r["post_id"],))
                log.warning("hidden sweep: our post %s was hidden by moderators (account %s)", r["post_id"], r["account_id"])
        never = sum(1 for r in rows if r["post_id"] not in hc)
        log.info("hidden sweep: checked %d of %d due (%d never checked), found %d hidden", len(batch), len(due),
                 never, found)
        if found:
            self.ensure_retirements()

    def track_some(self) -> None:
        # 12 a minute: we write ~3/min and each post is checked twice (25 min, 3 h), so 4 every 2 min fell
        # hours behind and starved the learner, strategist and STATUS counters of fresh data.
        self.tracker.run_once(12)

    def _later(self, seconds: float, fn: Callable, *args) -> None:
        self._seq += 1
        heapq.heappush(self.delayed, (time.time() + seconds, self._seq, fn, args))

    def _run_delayed(self) -> None:
        while self.delayed and self.delayed[0][0] <= time.time():
            _, _, fn, args = heapq.heappop(self.delayed)
            self._safe(lambda: fn(*args))

    def can_write(self) -> bool:
        return (not self.cfg.observe_only and time.time() >= self.not_ready_until
                and time.time() >= self.locked_until)

    # ======================================================================  reading
    def poll_latest(self) -> None:
        self.cls.ingest(self.p.feed("latest", 50))

    def poll_trending(self) -> None:
        posts = self.p.feed("trending", 50)
        self.cls.ingest(posts)
        self.spotlight_status(posts)
        self.db.put("trending_authors", list({p["author_id"] for p in posts}))

    def refresh_news(self) -> None:
        self.news = self.p.latest_news(8)

    def classify_some(self) -> None:
        fresh = self.db.all("""SELECT DISTINCT a.account_id FROM accounts a
                               WHERE a.classified_at IS NULL OR a.classified_at = 0
                               ORDER BY a.last_post_at DESC LIMIT 16""")
        ids = [r["account_id"] for r in fresh if r["account_id"] not in self.cls.our_ids]
        if len(ids) < 8:
            ids += [i for i in self.cls.needs_classification() if i not in ids][: 16 - len(ids)]
        if ids:
            self.cls.classify(ids[:16])

    def poll_notifications(self) -> None:
        if self.cfg.dry_run and not self.cfg.fake_platform:
            return
        rows = self.db.our_accounts("active")
        if not rows:
            return
        for _ in range(min(4, len(rows))):
            row = rows[self._notif_cursor % len(rows)]
            self._notif_cursor += 1
            try:
                notes = self.p.notifications(row)
            except AccountDead:
                continue
            if not notes:
                continue
            for n in notes:  # notification posts can omit author_id; reply/mention authors are the actor
                post = n.get("post")
                if post and post.get("author_id") is None and n.get("kind") in ("reply", "mention"):
                    aid = (n.get("actor") or {}).get("account_id")
                    if aid is not None:
                        n["post"] = dict(post, author_id=aid)
            self.cls.ingest([n["post"] for n in notes if n.get("post")])
            for n in notes:
                if n.get("kind") not in ("reply", "mention"):
                    continue
                actor = (n.get("actor") or {}).get("account_id") or (n.get("post") or {}).get("author_id")
                if actor is None or actor in self.cls.our_ids:
                    continue
                self.continuations[n["notification_id"]] = {
                    "account_id": row["account_id"], "actor": actor, "post": n["post"], "at": time.time()}
            self.p.resolve_notifications(row, max(n["notification_id"] for n in notes))

    # ======================================================================  accounts
    # The team may create 200 accounts in total (portal: "Maximum total 200", banned and deleted ones count).
    # Bans were using ~35 a day, which would exhaust the allowance around 29 Oct, before the x4 days.
    # So: keep a reserve for 1-2 Nov (x4) and pace new accounts until then; let the active count shrink if needed.
    FINAL_PUSH_START = 1793453400.0      # 2026-10-31 13:30 UTC = 1 Nov 00:00 Adelaide
    ACCOUNT_RESERVE = 35                 # accounts kept back for the x4 days
    ACTIVE_FLOOR = 18                    # below this, create even if ahead of pace (never touches the reserve early)

    def ensure_accounts(self) -> None:
        if self.cfg.observe_only or self.no_more_accounts or time.time() < self.not_ready_until:
            return
        self.ensure_retirements()
        now = time.time()
        active = len(self.db.our_accounts("active"))
        target = min(int(self.strategy["target_active_accounts"]), self.cfg.max_active_accounts)
        used = self.db.count_our_accounts()
        left = self.cfg.max_total_accounts - used
        if active >= target or left <= 0:
            return
        if now < self.FINAL_PUSH_START:
            spendable = left - self.ACCOUNT_RESERVE
            hours = max(1.0, (self.FINAL_PUSH_START - now) / 3600)
            made_2h = self.db.one("SELECT COUNT(*) c FROM our_accounts WHERE created_at > ?", (now - 7200,))["c"]
            allowed_2h = max(1.0, 2 * spendable / hours)
            if now - getattr(self, "_acct_note", 0) > 3600:
                self._acct_note = now
                log.info("ACCOUNTS active %d/%d, allowance left %d (reserve %d for 1-2 Nov), pace %.1f per 2 h, made %d in 2 h",
                         active, target, left, self.ACCOUNT_RESERVE, allowed_2h, made_2h)
            if spendable <= 0:
                return
            if made_2h >= allowed_2h and active >= self.ACTIVE_FLOOR:
                return
        self.create_account()

    def ensure_retirements(self) -> None:
        """No longer retires accounts. An account with a hidden post may be banned by another team's account
        report, but a retired account still counts against the 200-account allowance, and retiring one makes us
        create a replacement. Exposed accounts keep writing but never like, follow, report or boost (see _exposed)."""
        return
        for row in self.db.our_accounts("active"):
            # An account can only be suspended via an account report once a moderator has hidden one of its
            # posts AND it has 100+ actions. So retire exposed accounts well before 100 (the platform counts
            # some actions we don't see).
            exposed = self.db.one("SELECT COUNT(*) c FROM our_posts WHERE account_id=? AND status='hidden'",
                                  (row["account_id"],))["c"]
            if (exposed and row["actions"] >= self.cfg.retire_after_actions) or exposed >= 2:
                self.db.x("UPDATE our_accounts SET status='retired' WHERE account_id=?", (row["account_id"],))
                log.warning("retired @%s (%d actions, %d hidden posts)", row["username"], row["actions"], exposed)

    def create_account(self) -> None:
        used = {r["persona_key"]: 0 for r in self.db.all("SELECT persona_key FROM our_accounts")}
        for r in self.db.all("SELECT persona_key, COUNT(*) c FROM our_accounts GROUP BY persona_key"):
            used[r["persona_key"]] = r["c"]
        persona = min(PERSONAS, key=lambda p: (used.get(p["key"], 0), random.random()))
        if used.get(persona["key"], 0) == 0:
            first, last = persona["first"], persona["last"]
        else:
            first, last = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
        display = f"{first} {last}"
        password = make_password()
        if self.cfg.dry_run and not self.cfg.fake_platform:
            fake_id = -(self.db.count_our_accounts() + 1)
            self.db.add_our_account(fake_id, f"dry_{make_username(first, last)}", password, display, persona["key"], None)
            self.cls.refresh_our_ids()
            return
        for _ in range(3):
            username = make_username(first, last)
            try:
                acct = self.p.register(username, password, display)
            except NotReady:
                raise
            except Exception as e:
                status = getattr(e, "status", None)
                if status == 409:
                    continue
                if status == 403:
                    msg = (getattr(e, "message", "") or str(e)).lower()
                    if "active" in msg:
                        log.warning("active-account cap reached: %s — freeing a retired slot", msg)
                        self._free_active_slot()
                    elif "total" in msg or "allowance" in msg:
                        log.warning("total account allowance reached: %s", msg)
                        self.no_more_accounts = True
                    else:
                        log.warning("registration refused: %s", msg)
                        self.write_backoff_until = time.time() + 60
                    return
                raise
            self.db.add_our_account(acct.account_id, username, password, display, persona["key"], acct.token)
            with open(self.cfg.path("accounts_backup.csv"), "a") as fh:      # belt and braces: never lose a login
                fh.write(f"{acct.account_id},{username},{password},{display},{persona['key']}\n")
            self.cls.refresh_our_ids()
            log.info("created @%s (%s, persona %s)", username, display, persona["key"])
            return

    def _free_active_slot(self) -> None:
        row = self.db.one("SELECT * FROM our_accounts WHERE status='retired' ORDER BY last_used LIMIT 1")
        if row is None:
            self.no_more_accounts = True
            return
        try:
            self.p.session(row).delete()
        except Exception as e:
            log.info("could not delete retired @%s: %s", row["username"], e)
        self.db.x("UPDATE our_accounts SET status='deleted' WHERE account_id=?", (row["account_id"],))

    def pick_account(self, persona_options: Optional[list[str]] = None, exclude: set = frozenset(),
                     rest_seconds: float = 600):
        rows = [r for r in self.db.our_accounts("active") if r["account_id"] not in exclude]
        if not rows:
            return None
        # Rest by last WRITE, not last_used: likes, follows and reads also touch last_used, so with 40
        # accounts nobody ever looked "rested", the guard fell away and the persona bandit sent one
        # account 10+ posts in 20 minutes (looks like a bot, gets reported, and repeats itself).
        now = time.time()
        wrote = {w["account_id"]: w["t"] or 0 for w in self.db.all(
            "SELECT account_id, MAX(created_at) t FROM our_posts WHERE created_at > ? GROUP BY account_id",
            (now - 6 * 3600,))}
        rested = [r for r in rows if now - wrote.get(r["account_id"], 0) > rest_seconds]
        if not rested:                            # everyone wrote recently: take whoever wrote longest ago
            return min(rows, key=lambda r: (wrote.get(r["account_id"], 0), r["last_used"]))
        rows = rested
        if persona_options:
            keys = sorted({r["persona_key"] for r in rows})
            fit = [k for k in keys if k in persona_options] or keys
            chosen = self.bandit.choose("persona", fit, prefer=persona_options,
                                        weights=self.strategy["persona_weights"])
            rows = [r for r in rows if r["persona_key"] == chosen] or rows
        return min(rows, key=lambda r: (wrote.get(r["account_id"], 0), r["last_used"]))

    # ======================================================================  writing
    # Pipeline: the main thread picks WHAT to say (fast DB queries) and reserves the account and target;
    # worker threads write the text with the LLM in parallel; the main thread sends finished drafts the
    # moment a write slot opens. Before this, every write waited ~25 s on the LLM, so we used ~2 of the
    # 3 writes a minute the team is allowed.
    KIND_ORDER = {"continuation": 0, "boost_reply": 1, "boost_repost": 1, "reply": 2, "post": 3, "life": 4}
    # Conversations are where citizen points come from: in a live sample, 161 of 181 citizen replies (89%) answered a
    # bot reply addressed to that citizen, mostly 4-8 messages deep, and the accounts earning the most citizen replies
    # write nothing but follow-ups. So we follow up with everyone; we only stop after two strong pushbacks in a row.
    CONTINUE_MIN_SHIFT = -0.7      # a strong pushback
    CONTINUE_MAX_PUSHBACK = 2      # stop the thread after this many strong pushbacks from the same citizen
    NIGHT_SKIP = 0.6               # share of empty night slots left unused (no standalone post)
    NIGHT_SKIP_SECONDS = 20        # one write slot (team limit 3 writes a minute)
    # The random skip alone did not bind: drafts are planned ahead (queue of 3), so even with 60% of plans skipped the
    # planner still supplied >3 posts a minute and night POSTs stayed ~75/h. A hard gap between fallback posts does bind.
    NIGHT_POST_GAP = 90            # seconds between fallback standalone posts at night (<= ~40/h)
    NIGHT_UTC = range(13, 21)      # 23:30-07:30 Adelaide: citizens post 5-7x less; save accounts, keep replying

    def write_slot(self) -> None:
        self._harvest_drafts()
        if not self.can_write() or time.time() < self.write_backoff_until:
            return
        if not self.db.our_accounts("active"):
            return
        if self.ready and self.limits.write.available():
            self._send_next()
        if (len(self.inflight) + len(self.ready) < self.cfg.draft_queue
                and time.time() >= self.plan_backoff_until):
            job = self._plan()
            if job is None:
                # nothing worth saying right now; don't spin. A night skip holds planning for a whole write slot,
                # otherwise the 6 s retry re-rolls the skip ~3 times per slot and most "skipped" slots still post.
                self.plan_backoff_until = max(time.time() + 6, self._skip_slot_until)
                return
            self._reserve(job)
            job["planned_at"] = time.time()
            self.inflight.append((self.drafter.submit(self._compose_safe, job), job))

    def is_night(self) -> bool:
        return time.gmtime().tm_hour in self.NIGHT_UTC

    def _plan(self):
        night = self.is_night()
        share = float(self.strategy["share_top_level"]) * (0.3 if night else 1.0)
        job = (self.plan_continuation()
               or self.plan_boost()
               or (random.random() < share and self.plan_top_level())
               or self.plan_reply())
        if job:
            return job
        # Nobody fresh to talk to. Citizens read their notifications next session, so a reply to a post up to
        # 30 min old still lands; a standalone post at night mostly earns reports.
        job = self.plan_reply(fresh_seconds=1800)
        if job:
            return job
        if night and (random.random() < self.NIGHT_SKIP
                      or time.time() - self._last_night_post < self.NIGHT_POST_GAP):
            self._skip_slot_until = time.time() + self.NIGHT_SKIP_SECONDS
            return None
        job = self.plan_top_level()
        if job and night:
            self._last_night_post = time.time()
        return job

    def _reserve(self, job) -> None:
        self.busy_accounts.add(job["row"]["account_id"])
        if job.get("target_post_id"):
            self.busy_posts.add(job["target_post_id"])
        for aid in [job.get("target_author")] + list(job.get("mentions") or []):
            if aid:
                self.busy_people.add(aid)

    def _release(self, job) -> None:
        self.busy_accounts.discard(job["row"]["account_id"])
        self.busy_posts.discard(job.get("target_post_id"))
        for aid in [job.get("target_author")] + list(job.get("mentions") or []):
            self.busy_people.discard(aid)

    def _harvest_drafts(self) -> None:
        still = []
        for fut, job in self.inflight:
            if not fut.done():
                still.append((fut, job))
                continue
            res = None
            try:
                res = fut.result()
            except Exception as e:                       # _compose_safe already logs; belt and braces
                log.warning("draft failed: %s", e)
            if res and res.get("text"):
                res["drafted_at"] = time.time()
                self.ready.append(res)
            else:
                self._release(job)
        self.inflight = still

    def _compose_safe(self, job):
        try:
            return {"continuation": self.compose_continuation, "reply": self.compose_reply,
                    "post": self.compose_top_level, "life": self.compose_life,
                    "boost_reply": self.compose_boost_reply, "boost_repost": lambda j: dict(j, text="(repost)", model="none"),
                    }[job["kind"]](job)
        except Exception as e:
            log.warning("drafting %s failed: %s", job.get("kind"), e)
            return None

    def _send_next(self) -> None:
        now = time.time()
        fresh = []
        for j in self.ready:
            if j["kind"] in ("reply", "continuation") and now - j["drafted_at"] > self.cfg.draft_max_age:
                self._release(j)                          # the conversation has moved on
            else:
                fresh.append(j)
        self.ready = fresh
        if not self.ready:
            return
        self.ready.sort(key=lambda j: (self.KIND_ORDER.get(j["kind"], 9), j["drafted_at"]))
        job = self.ready.pop(0)
        try:
            row = self.db.one("SELECT * FROM our_accounts WHERE account_id=? AND status='active'",
                              (job["row"]["account_id"],))
            if row is None:
                return
            if job["kind"] == "boost_repost":
                self._send_boost_repost(row, job)
            elif job["kind"] == "boost_reply":
                if self._send_reply(row, job["target"], job["text"], "boost", None, None, job["model"]):
                    job["spot"]["replies_done"] += 1
                    job["spot"]["reply_texts"].append(job["text"])
            elif job["kind"] in ("reply", "continuation"):
                if self._send_reply(row, job["target"], job["text"], job["kind"], job.get("arg"),
                                    job["target_author"], job["model"], mentions=job.get("mentions") or []):
                    self._engage(row, job)
            else:
                try:
                    out = self.p.post(row, job["text"])
                except AccountDead:
                    return
                self._record(out, row, job["kind"], job.get("arg"), None, None, job["text"], job["model"],
                             job.get("mentions") or [])
                if job["kind"] == "post" and self._spotlight_due() and (job.get("spot_candidate") or self._spotlight_overdue()):
                    self.start_spotlight(out, row, job["text"])
                elif job["kind"] == "post":
                    for i in range(int(self.strategy["boost_likes_per_post"])):
                        self._later(random.uniform(60, 420) * (i + 1), self._boost_like, out.get("post_id"),
                                    row["account_id"])
        finally:
            self._release(job)

    # ---- 0. spotlight: push one of our posts into Trending ------------------
    # Trending is where citizens see posts (top Trending posts get 100-300 citizen views; our ordinary posts get ~3).
    # Its hot score is roughly 2.5*ln(1+likes) + 2.5*ln(1+replies) + 2.7*ln(1+reposts) + 0.5*ln(1+views) - 1.26*age_h,
    # so every top team stacks likes, reposts and replies from its own accounts on a few posts. Likes have their own
    # 30/min budget; reposts and replies come out of the 3 writes/min, so we spotlight one post every ~20 minutes.
    def _spotlight_due(self) -> bool:
        every = int(self.strategy["spotlight_every_min"])
        return every > 0 and not self.is_night() and time.time() - self._last_spotlight >= every * 60

    def _spotlight_overdue(self, grace_min: int = 15) -> bool:
        """No drafted spotlight candidate turned up in time: spotlight the next ordinary post instead."""
        every = int(self.strategy["spotlight_every_min"])
        return every > 0 and time.time() - self._last_spotlight >= (every + grace_min) * 60

    def start_spotlight(self, out, row, text) -> None:
        pid = (out or {}).get("post_id")
        if not pid:
            return
        self._last_spotlight = time.time()
        self.db.put("last_spotlight", self._last_spotlight)
        spot = {"post_id": pid, "root": pid, "author": row["account_id"], "body": text, "started": time.time(),
                "used": {row["account_id"]}, "used_reply": {row["account_id"]}, "used_repost": {row["account_id"]},
                "reposts_left": int(self.strategy["spotlight_reposts"]),
                "replies_left": int(self.strategy["spotlight_replies"]), "replies_done": 0, "reposts_done": 0,
                "reply_texts": [], "likes": 0}
        self.spotlights.append(spot)
        self.spotlights = self.spotlights[-6:]
        exposed = self._exposed()
        likers = [r for r in self.db.our_accounts("active") if r["account_id"] != row["account_id"]
                  and r["account_id"] not in exposed]
        random.shuffle(likers)
        for i, r in enumerate(likers[: int(self.strategy["spotlight_likes"])]):
            self._later(4 + i * 2.3 + random.uniform(0, 1.5), self._spot_like, dict(r), pid, spot)
        log.info("SPOTLIGHT start post %s by @%s: %d likes, %d reposts, %d replies queued", pid, row["username"],
                 min(len(likers), int(self.strategy["spotlight_likes"])), spot["reposts_left"], spot["replies_left"])

    def _spot_like(self, row, post_id, spot) -> None:
        if not self.limits.like.available():
            self._later(3, self._spot_like, row, post_id, spot)       # likes have their own budget; wait our turn
            return
        try:
            self.p.like(row, post_id)
            spot["likes"] += 1
        except Exception as e:
            log.debug("spotlight like failed: %s", e)

    def plan_boost(self):
        """Reposts and supportive replies for live spotlights, spread over their first ~40 minutes."""
        now = time.time()
        live = [s for s in self.spotlights if now - s["started"] < 45 * 60 and (s["reposts_left"] > 0 or s["replies_left"] > 0)]
        if not live:
            return None
        spot = live[0]
        # pace: about 3 in 5 write slots while a spotlight is live, so it peaks within ~30 minutes
        if random.random() > 0.6:
            return None
        do_reply = spot["replies_left"] > 0 and (spot["reposts_left"] <= 0 or random.random() < 0.45)
        used = spot["used_reply"] if do_reply else spot["used_repost"]      # each account: one reply and one repost
        exclude = set(self.busy_accounts) | used | self._exposed()
        row = self.pick_account(exclude=exclude, rest_seconds=120)
        if row is None or row["account_id"] in exclude:
            return None
        used.add(row["account_id"])
        if do_reply:
            spot["replies_left"] -= 1
            target = {"post_id": spot["post_id"], "root_post_id": spot["root"], "body": spot["body"],
                      "reply_to_post_id": None, "author_id": spot["author"]}
            return {"kind": "boost_reply", "row": dict(row), "spot": spot, "target": target,
                    "target_post_id": None, "target_author": None}
        spot["reposts_left"] -= 1
        return {"kind": "boost_repost", "row": dict(row), "spot": spot, "target_post_id": None, "target_author": None}

    def compose_boost_reply(self, job):
        row, spot = job["row"], job["spot"]
        text, model = self.writer.spotlight_reply(persona_key=row["persona_key"], display_name=row["display_name"],
                                                  post_body=spot["body"], earlier=spot["reply_texts"])
        if not text:
            return None
        job.update(text=self._humanize(text), model=model)
        return job

    def _send_boost_repost(self, row, job) -> None:
        spot = job["spot"]
        try:
            out = self.p.repost(row, spot["post_id"])
        except AccountDead:
            return
        except Exception as e:
            log.info("spotlight repost failed: %s", e)
            return
        spot["reposts_done"] += 1
        pid = (out or {}).get("post_id")
        if pid:
            self.db.x("""INSERT OR REPLACE INTO our_posts(post_id,account_id,persona_key,kind,argument,target_id,
                         target_post,mentions,body,model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (pid, row["account_id"], row["persona_key"], "repost", None, None, spot["post_id"], "[]",
                       "", "none", time.time()))

    def spotlight_status(self, trending: list) -> None:
        mine = {s["post_id"]: s for s in self.spotlights}
        ranks = {p["post_id"]: (i + 1, round(p.get("global_hot_score") or 0, 1)) for i, p in enumerate(trending)}
        for pid, s in mine.items():
            if time.time() - s["started"] > 4 * 3600:
                continue
            rk = ranks.get(pid)
            last = s.get("_logged_rank")
            if rk != last and (rk or last):
                s["_logged_rank"] = rk
                log.info("SPOTLIGHT status post %s: trending %s (age %.0f min, likes %d, reposts %d, replies %d)", pid,
                         f"#{rk[0]} hot {rk[1]}" if rk else "not in top 50", (time.time() - s["started"]) / 60,
                         s["likes"], s["reposts_done"], s["replies_done"])

    # ---- privacy: hide join date, likes and history from other teams' blue bots --------------------------------
    def ensure_private(self) -> None:
        for row in self.db.our_accounts("active"):
            if row["account_id"] in self._privacy_done:
                continue
            try:
                self.p.set_private(row)
                self._privacy_done.add(row["account_id"])
                self.db.put("private_accounts", sorted(self._privacy_done))
                log.info("made @%s private", row["username"])
            except Exception as e:
                log.info("could not make @%s private: %s", row["username"], e)
            return                                   # one per tick

    # ---- 1. conversations ------------------------------------------------
    def plan_continuation(self):
        now = time.time()
        for nid in list(self.continuations):
            if now - self.continuations[nid]["at"] > 15 * 60:
                del self.continuations[nid]
        deferred, job = {}, None
        while self.continuations:
            nid = max(self.continuations, key=lambda k: self.continuations[k]["at"])
            item = self.continuations.pop(nid)
            if item["actor"] in self.busy_people or item["account_id"] in self.busy_accounts:
                deferred[nid] = item                      # already talking to them; pick it up next time
                continue
            row = self.db.one("SELECT * FROM our_accounts WHERE account_id=? AND status='active'",
                              (item["account_id"],))
            if not row:
                continue
            if self._pushbacks.get(item["actor"], 0) >= self.CONTINUE_MAX_PUSHBACK:
                continue                                  # they've pushed back hard twice; leave it there
            job = {"kind": "continuation", "row": dict(row), "item": item, "target_author": item["actor"],
                   "target_post_id": (item.get("post") or {}).get("post_id")}
            break
        self.continuations.update(deferred)
        return job

    def compose_continuation(self, job):
        item, row = job["item"], job["row"]
        actor = item["actor"]
        if not self._is_npc(actor):
            return None
        if actor > NPC_ID_CUTOFF and random.random() >= float(self.strategy["noncitizen_target_weight"]):
            return None          # not a citizen: a continuation scores nothing, spend the slot on someone who counts
        prof = self._profile(actor)
        their = item["post"]
        try:
            full = self.p.read("get_post", their["post_id"])
        except Exception:
            full = their
        thread, depth = self.thread_context(full)
        if depth is not None and depth >= self.cfg.max_thread_depth:
            return None
        ours = self.db.one("SELECT body FROM our_posts WHERE post_id=?", (full.get("reply_to_post_id"),))
        # Arguing on with someone who is pushing back mostly gets the No case restated at us (and risks moving them
        # further away, -80). Continuations to pushback scored 4 up vs 11 down; first-touch replies 23 vs 14. So
        # leave pushback alone, rest them from targeting for a while, and spend the write slot on someone new.
        shift, _ = self.writer.judge_shift(ours["body"] if ours else "", full.get("body") or "")
        if shift <= self.CONTINUE_MIN_SHIFT:
            self._pushbacks[actor] = self._pushbacks.get(actor, 0) + 1
            if self._pushbacks[actor] >= self.CONTINUE_MAX_PUSHBACK:
                self.db.x("UPDATE accounts SET last_targeted=? WHERE account_id=?", (time.time(), actor))
                log.info("ending thread with %s: pushed back hard %d times (shift %.2f)", actor, self._pushbacks[actor], shift)
                return None
        elif shift > 0:
            self._pushbacks.pop(actor, None)
        text, model = self.writer.continuation(
            persona_key=row["persona_key"], display_name=row["display_name"],
            our_post=(ours["body"] if ours else ""), their_reply=full.get("body") or "",
            target_profile=dict(prof) if prof else None, thread=thread)
        if not text:
            return None
        job.update(text=text, model=model, target=full, arg=None, mentions=[])
        return job

    # ---- 2. replies to fresh NPC posts -------------------------------------
    def plan_reply(self, fresh_seconds: Optional[int] = None):
        now = time.time()
        fresh_seconds = int(fresh_seconds or self.strategy["reply_fresh_seconds"])
        rows = self.db.all("""
            SELECT p.post_id, p.author_id, p.kind, p.body, p.reply_to, p.root_id, p.created_at, p.depth,
                   a.npc_score, a.stance, a.stance_vacc, a.stance_yearly, a.concerns, a.identity, a.persuadable,
                   a.username, a.moved_until, a.last_targeted
            FROM posts p JOIN accounts a ON a.account_id = p.author_id
            WHERE p.created_at > ? AND p.kind IN ('post','reply') AND p.body IS NOT NULL AND length(p.body) > 25
              AND a.npc_score >= 0.7 AND COALESCE(a.moved_until,0) < ? AND COALESCE(a.last_targeted,0) < ?
              AND (p.depth IS NULL OR p.depth < ?)
              AND p.author_id NOT IN (SELECT account_id FROM our_accounts)
              AND p.post_id NOT IN (SELECT COALESCE(target_post,0) FROM our_posts)
            ORDER BY (p.author_id <= ?) DESC, p.created_at DESC LIMIT 80""",
                           (now - fresh_seconds, now, now - self.cfg.target_cooldown_minutes * 60,
                            self.cfg.max_thread_depth, NPC_ID_CUTOFF))
        rows = [r for r in rows if r["post_id"] not in self.skip_posts and r["post_id"] not in self.busy_posts
                and r["author_id"] not in self.busy_people]
        if not rows:
            return None
        # Who started each thread? The accounts that earn the most citizen replies mostly reply inside threads the
        # citizen started (their own post, or their replies within it); a citizen talking to another bot is a
        # worse bet. Top-level citizen posts have no root.
        roots = {r["root_id"] for r in rows if r["root_id"]}
        root_author = {}
        if roots:
            q = ",".join("?" * len(roots))
            root_author = {x["post_id"]: x["author_id"] for x in
                           self.db.all(f"SELECT post_id, author_id FROM posts WHERE post_id IN ({q})", tuple(roots))}
        def cit_rooted(r):
            if not r["root_id"] or r["root_id"] == r["post_id"]:
                return True
            ra = root_author.get(r["root_id"])
            return ra is not None and ra <= NPC_ID_CUTOFF
        score, r = max(((self._target_score(r, now, cit_rooted(r)), r) for r in rows), key=lambda t: t[0])
        if score <= 0.05:
            return None
        concerns = json.loads(r["concerns"] or "[]") or ["other"]
        row = self.pick_account(personas_for(concerns, [p["key"] for p in PERSONAS]), exclude=self.busy_accounts)
        if row is None:
            return None
        if self._life_post_due(row):
            return {"kind": "life", "row": dict(row)}
        arg = self.bandit.choose("arg", list(ARGUMENTS), prefer=arguments_for(concerns),
                                 weights=self.strategy["argument_weights"])
        smart = (random.random() < self.strategy["smart_all_writing"]) or (
            (r["stance"] in (-1, 0, None)) and
            (r["persuadable"] or 0.5) >= self.strategy["smart_reply_min_persuadable"])
        target = {"post_id": r["post_id"], "root_post_id": r["root_id"], "body": r["body"],
                  "reply_to_post_id": r["reply_to"], "author_id": r["author_id"]}
        return {"kind": "reply", "row": dict(row), "r": dict(r), "target": target, "concerns": concerns,
                "arg": arg, "smart": smart, "target_author": r["author_id"], "target_post_id": r["post_id"]}

    def compose_reply(self, job):
        r, row, target = job["r"], job["row"], job["target"]
        thread, depth = self.thread_context(target)
        if depth is not None and depth >= self.cfg.max_thread_depth:
            self.db.x("UPDATE posts SET depth=? WHERE post_id=?", (depth, r["post_id"]))
            return None
        text, model = self.writer.reply(
            persona_key=row["persona_key"], display_name=row["display_name"], target_post=target,
            thread=thread, target_profile=r, argument=job["arg"], smart=job["smart"])
        if not text:
            self.skip_posts.add(r["post_id"])
            return None
        text = self._humanize(text)
        extra = self._co_targets(job["concerns"], exclude={r["author_id"]} | set(self.busy_people),
                                 n=int(self.strategy["extra_mentions_per_reply"]))
        if extra:
            text = _with_mentions(text, [u for _, u in extra], 3)
        job.update(text=text, model=model, mentions=[a for a, _ in extra])
        return job

    def _target_score(self, r, now: float, citizen_rooted: bool = True) -> float:
        sw = self.strategy["stance_weights"]
        stance_w = float(sw.get(str(r["stance"]), 0.7)) if r["stance"] is not None else 0.7
        concerns = json.loads(r["concerns"] or "[]")
        concern_w = max([self.strategy.weight("concern_weights", c) for c in concerns] or [1.0])
        if r["stance"] == 2 and ((r["stance_yearly"] or 0) < 0 or (r["stance_vacc"] or 0) < 0):
            stance_w = 0.5            # yearly/general vaccination views also pay 160
        age = max(0.0, now - (r["created_at"] or now))
        fresh = math.exp(-age / 150.0)
        # Citizens keep talking 4-8 deep in their own threads, so depth only counts against us in someone else's.
        depth_w = 1.0 if citizen_rooted else max(0.3, 0.6 - 0.1 * (r["depth"] or 0))
        trending = 1.15 if r["author_id"] in set(self.db.get("trending_authors", []) or []) else 1.0
        on_topic = 1.0 if ON_TOPIC.search(r["body"] or "") else 0.15     # don't hijack footy posts
        # Only citizens score, and every citizen was created before the game opened (id <= NPC_ID_CUTOFF). ~94% of
        # the live feed is other teams' accounts that the classifier often rates as "NPC"; replying to them earns
        # nothing and draws No-side rebuttals. Down-weight them so a citizen wins whenever one is in the pool.
        citizen_w = 1.0 if r["author_id"] <= NPC_ID_CUTOFF else float(self.strategy["noncitizen_target_weight"])
        return citizen_w * (r["npc_score"] or 0) * (r["persuadable"] if r["persuadable"] is not None else 0.5 + 0.0) * \
            stance_w * concern_w * fresh * depth_w * trending * on_topic + 0.01

    # ---- 3. top-level posts ----------------------------------------------
    def plan_top_level(self):
        now = time.time()
        active = self.db.all("""SELECT account_id, username, concerns FROM accounts
                                WHERE npc_score >= 0.7 AND last_post_at > ? AND COALESCE(stance,0) BETWEEN -1 AND 1
                                  AND COALESCE(last_targeted,0) < ? AND username IS NOT NULL
                                  AND account_id NOT IN (SELECT account_id FROM our_accounts)""",
                             (now - 30 * 60, now - self.cfg.target_cooldown_minutes * 60))
        counts: dict[str, int] = {}
        for a in active:
            for c in json.loads(a["concerns"] or "[]"):
                counts[c] = counts.get(c, 0) + 1
        for c in counts:
            counts[c] *= self.strategy.weight("concern_weights", c)
        recent = self.db.get("recent_post_concerns", []) or []
        for c in recent[-3:]:                     # rotate topics across standalone posts
            if c in counts:
                counts[c] *= 0.3
        concern = max(counts, key=counts.get) if counts else random.choice(list(CONCERNS)[:12])
        self.db.put("recent_post_concerns", (recent + [concern])[-6:])
        mentions = [m for m in self._co_targets([concern], exclude=set(self.busy_people),
                                                n=int(self.strategy["mentions_per_post"]), window=5 * 60)]
        row = self.pick_account(personas_for([concern], [p["key"] for p in PERSONAS]), exclude=self.busy_accounts)
        if row is None:
            return None
        if self._life_post_due(row):
            return {"kind": "life", "row": dict(row)}
        # Standalone posts make the positive case; conceding the other side's points is for replies only.
        options = [a for a in ARGUMENTS if a != "concede_and_pivot"]
        arg = self.bandit.choose("arg", options, prefer=[a for a in arguments_for([concern]) if a in options],
                                 weights=self.strategy["argument_weights"])
        relevant = [a for a in self.news
                    if ON_TOPIC.search(f"{a.get('title', '')} {a.get('summary') or a.get('excerpt') or ''}")]
        news = random.choice(relevant) if relevant and random.random() < 0.5 else None
        # The spotlit post is the one ~100 citizens actually see (Trending top 10), so it carries our best-scoring
        # standalone argument rather than whatever the bandit happens to explore next.
        spot_candidate = self._spotlight_due()
        if spot_candidate:
            arg = self.bandit.best("arg", options) or arg
            news = None
        return {"kind": "post", "row": dict(row), "arg": arg, "news": news, "concern": concern,
                "mention_pairs": mentions, "mentions": [a for a, _ in mentions], "spot_candidate": spot_candidate}

    def _recent_our_posts(self, n: int = 8) -> list:
        """Bodies of our own most recent standalone posts (all accounts), newest first."""
        try:
            rows = self.db.all("""SELECT body FROM our_posts WHERE kind='post' AND body IS NOT NULL
                                  AND created_at > ? ORDER BY created_at DESC LIMIT ?""",
                               (time.time() - 3600, n))
            return [r["body"] for r in rows]
        except Exception:
            return []

    def compose_top_level(self, job):
        row, news = job["row"], job.get("news")
        text, model = self.writer.top_level(persona_key=row["persona_key"], display_name=row["display_name"],
                                            argument=job["arg"], news=news, audience_concern=CONCERNS[job["concern"]],
                                            smart=random.random() < self.strategy["smart_all_writing"],
                                            recent_ours=self._recent_our_posts())
        if not text:
            return None
        if NO_TALKING_POINTS.search(text):
            log.info("dropped standalone draft that repeats the other side's talking points")
            return None
        text = self._humanize(text)
        if self.yes_tags and random.random() < self.strategy["hashtag_rate"] and "#" not in text:
            text = f"{text} #{random.choice(self.yes_tags)}"
        if news and news.get("url") and len(text) + len(news["url"]) < 440:
            text = f"{text}\n\n{news['url']}"
        elif random.random() < self.strategy["gif_rate"]:
            text = self._with_gif(text)
        if job["mention_pairs"]:
            text = _with_mentions(text, [u for _, u in job["mention_pairs"]], 4)
        job.update(text=text, model=model)
        return job

    def compose_life(self, job):
        row = job["row"]
        text, model = self.writer.life_post(persona_key=row["persona_key"], display_name=row["display_name"])
        if not text:
            return None
        text = self._humanize(text)
        if random.random() < min(0.5, self.strategy["gif_rate"] * 2):
            text = self._with_gif(text)
        job.update(text=text, model=model, mentions=[])
        return job

    # ---- extra reach (likes and follows have their own budgets, separate from writes) -------------------
    def _engage(self, row, job) -> None:
        """Like the post we just answered, and follow citizens who keep talking with us (they may follow back)."""
        if row["account_id"] in self._exposed():
            return
        target_post = job.get("target_post_id") or (job.get("target") or {}).get("post_id")
        if target_post and random.random() < self.strategy["like_reply_targets"]:
            self._later(random.uniform(5, 45), self._quiet_like, dict(row), target_post)
        actor = job.get("target_author")
        if job["kind"] == "continuation" and actor and random.random() < self.strategy["follow_engaged"]:
            self._later(random.uniform(30, 180), self._quiet_follow, dict(row), actor)

    def _quiet_like(self, row, post_id) -> None:
        if not post_id or not self.limits.like.available():
            return
        try:
            self.p.like(row, post_id)
        except Exception as e:
            log.debug("like failed: %s", e)

    def _quiet_follow(self, row, account_id) -> None:
        if self.db.one("SELECT 1 FROM follows WHERE account_id=? AND target_id=?", (row["account_id"], account_id)):
            return
        if not self.limits.follow.available():
            return
        try:
            self.p.follow(row, account_id)
        except Exception as e:
            log.debug("follow failed: %s", e)
            return
        self.db.x("INSERT OR IGNORE INTO follows(account_id,target_id,created_at) VALUES(?,?,?)",
                  (row["account_id"], account_id, time.time()))

    # ---- blending in -------------------------------------------------------
    def refresh_gifs_and_tags(self) -> None:
        """GIFs real citizens have posted (with the words around them), and popular Yes-friendly hashtags."""
        rows = self.db.all("""SELECT p.body FROM posts p JOIN accounts a ON a.account_id = p.author_id
                              WHERE p.body LIKE '%giphy.com%' AND a.npc_score >= 0.7
                              ORDER BY p.created_at DESC LIMIT 300""")
        pool = {}
        for r in rows:
            for url in GIF_URL.findall(r["body"] or ""):
                words = _content_words(GIF_URL.sub(" ", r["body"]))
                if words:
                    pool[url.rstrip(".,)")] = words
        self.gif_pool = list(pool.items())
        try:
            tags = [t.get("name", "").lower() for t in self.p.read("popular_tags")]
        except Exception:
            tags = []
        self.yes_tags = [t for t in tags if t in YES_TAGS] or ["SupportOurNurses", "ProofKeepsUsOpen"]
        log.info("blend-in: %d citizen GIFs, tags %s", len(self.gif_pool), self.yes_tags)

    def _with_gif(self, text: str) -> str:
        """Append a GIF a citizen used in a similar context, or nothing if none fits."""
        if not self.gif_pool or "giphy" in text:
            return text
        mine = _content_words(text)
        best, overlap = None, 0
        for url, words in self.gif_pool:
            o = len(mine & words)
            if o > overlap:
                best, overlap = url, o
        if best and overlap >= 2 and len(text) + len(best) < 470:
            return f"{text} {best}"
        return text

    def _humanize(self, text: str) -> str:
        if text and random.random() < self.strategy["lowercase_rate"] and not text.startswith(("I ", "I'")):
            text = text[0].lower() + text[1:]
        return text

    def _life_post_due(self, row) -> bool:
        n = self.cfg.life_post_every
        return n > 0 and row["posts_made"] > 0 and row["posts_made"] % n == n - 1

    def _exposed(self) -> set:
        """Accounts with a moderator-hidden post: one account report away from suspension at 100 actions,
        so they only spend actions on posts that earn points, never on likes, follows or reports."""
        return {r["account_id"] for r in self.db.all(
            "SELECT DISTINCT account_id FROM our_posts WHERE status='hidden'")}

    def _boost_like(self, post_id, author_account_id) -> None:
        if not post_id or post_id >= 9_000_000_000:
            return
        row = self.pick_account(exclude={author_account_id} | self._exposed())
        if row is not None:
            try:
                self.p.like(row, post_id)
            except Exception as e:
                log.debug("boost like failed: %s", e)

    # ---- helpers -----------------------------------------------------------
    def _send_reply(self, row, target, text, kind, arg, target_author, model, mentions=None) -> bool:
        try:
            out = self.p.reply(row, target, text)
        except AccountDead:
            log.warning("@%s is suspended — dropping it", row["username"])
            return False
        except Exception as e:
            if getattr(e, "status", None) in (404, 410, 400):
                log.info("reply target %s unavailable: %s", target.get("post_id"), e)
                self.db.x("UPDATE posts SET status='gone' WHERE post_id=?", (target.get("post_id"),))
                return False
            raise
        self._record(out, row, kind, arg, target_author, target.get("post_id"), text, model, mentions or [])
        return True

    def _record(self, out, row, kind, arg, target_author, target_post, text, model, mentions) -> None:
        now = time.time()
        pid = (out or {}).get("post_id")
        if pid:
            self.db.x("""INSERT OR REPLACE INTO our_posts(post_id,account_id,persona_key,kind,argument,target_id,
                         target_post,mentions,body,model,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (pid, row["account_id"], row["persona_key"], kind, arg, target_author, target_post,
                       json.dumps(mentions), text, model, now))
        for aid in ([target_author] if target_author else []) + list(mentions):
            self.db.x("UPDATE accounts SET last_targeted=?, times_targeted=times_targeted+1 WHERE account_id=?",
                      (now, aid))
        if arg:
            self.bandit.pull("arg", arg)
        self.bandit.pull("persona", row["persona_key"])
        self.bandit.pull("kind", kind)
        self.bandit.pull("gif", "yes" if "giphy" in (text or "") else "no")
        log.info("%s @%s [%s/%s, %s]: %s", kind.upper(), row["username"], row["persona_key"], arg, model,
                 text.replace("\n", " ")[:200])

    def _is_npc(self, account_id: int) -> bool:
        if account_id <= NPC_ID_CUTOFF:
            return True
        r = self.db.one("SELECT npc_score FROM accounts WHERE account_id=?", (account_id,))
        if r is None or r["npc_score"] is None:
            self.db.x("INSERT OR IGNORE INTO accounts(account_id, first_seen) VALUES(?,?)", (account_id, time.time()))
            self.cls.classify([account_id])
            r = self.db.one("SELECT npc_score FROM accounts WHERE account_id=?", (account_id,))
        return bool(r and (r["npc_score"] or 0) >= 0.6)

    def _profile(self, account_id: int):
        return self.db.one("SELECT * FROM accounts WHERE account_id=?", (account_id,))

    def _co_targets(self, concerns: list[str], exclude: set, n: int, window: int = 4 * 60) -> list[tuple[int, str]]:
        if n <= 0:
            return []
        now = time.time()
        rows = self.db.all("""SELECT account_id, username, concerns FROM accounts
                              WHERE npc_score >= 0.75 AND last_post_at > ? AND username IS NOT NULL
                                AND COALESCE(stance,0) BETWEEN -1 AND 1 AND COALESCE(last_targeted,0) < ?
                                AND COALESCE(moved_until,0) < ?
                                AND account_id NOT IN (SELECT account_id FROM our_accounts)
                              ORDER BY last_post_at DESC LIMIT 60""",
                           (now - window, now - self.cfg.target_cooldown_minutes * 60, now))
        out = []
        for r in rows:
            if r["account_id"] in exclude:
                continue
            if set(json.loads(r["concerns"] or "[]")) & set(concerns):
                out.append((r["account_id"], r["username"]))
            if len(out) >= n:
                break
        return out

    def thread_context(self, post: dict, max_up: int = 8) -> tuple[list[dict], Optional[int]]:
        """Walk up the thread (cached in SQLite). Returns oldest-first context and the post's depth."""
        chain, cur, depth = [], post, 0
        ours = self.cls.our_ids
        while cur and cur.get("reply_to_post_id") and depth < max_up:
            pid = cur["reply_to_post_id"]
            row = self.db.one("SELECT post_id, author_id, body, reply_to FROM posts WHERE post_id=?", (pid,))
            if row:
                parent = {"post_id": row["post_id"], "author_id": row["author_id"], "body": row["body"] or "",
                          "reply_to_post_id": row["reply_to"]}
            else:
                try:
                    parent = self.p.read("get_post", pid)
                    self.cls.ingest([parent])
                except Exception:
                    break
            who = "you" if parent.get("author_id") in ours else ("citizen" if parent.get("author_id", 10**9) <= NPC_ID_CUTOFF
                                                                  else "someone")
            if len(chain) < 4:
                chain.append({"who": who, "body": parent.get("body") or ""})
            cur = parent
            depth += 1
        complete = not (cur and cur.get("reply_to_post_id"))
        return list(reversed(chain)), (depth if complete else max(depth, max_up))

    # ======================================================================  blue
    def blue_tick(self) -> None:
        if self.cfg.observe_only or not self.can_write():
            return
        row = self.pick_account(exclude=self._exposed())
        if row is not None and self.limits.report_post.available():
            made = self.blue.report_some(row, max_reports=1)
            if not made and time.time() - getattr(self, "_blue_note", 0) > 900:
                self._blue_note = time.time()
                log.info("blue: no report this round (candidates=%d, pending=%d, blocked=%s)",
                         len(self.blue.candidates(limit=50)), self.blue.pending_count(), self.blue.blocked())

    def blue_accounts_tick(self) -> None:
        if self.cfg.observe_only or not self.can_write():
            return
        row = self.pick_account(exclude=self._exposed())
        if row is not None:
            self.blue.report_accounts(row, max_reports=2)

    # ======================================================================  status
    def status_line(self) -> None:
        log.info("STATUS %s", json.dumps(status(self.db)))

    def report_line(self) -> None:
        log.info("REPORT %s", compact(build_report(self.db, self.strategy, hours=24)))

    def strategist_tick(self) -> None:
        if self.strategist.due():
            self.strategist.run()
            self.report_line()

    def miner_tick(self, every: float = 2.5 * 3600) -> None:
        import threading
        if self._miner_thread is not None and self._miner_thread.is_alive():
            return
        if time.time() - self._miner_last < every:
            return
        self._miner_last = time.time()

        def work():
            try:
                self.miner.run_once()
            except Exception as e:
                log.warning("miner failed: %s", e)

        self._miner_thread = threading.Thread(target=work, name="miner", daemon=True)
        self._miner_thread.start()

    def refresh_team_info(self) -> None:
        if self.cfg.dry_run or self.cfg.fake_platform:
            return
        rows = self.db.our_accounts("active")
        if not rows:
            return
        try:
            me = self.p.session(rows[0]).me()
            team = me.get("team")
            if team:
                self.db.put("team_info", team)
                log.info("TEAM %s", json.dumps(team, default=str)[:500])
        except Exception as e:
            log.debug("team info failed: %s", e)


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z']{4,}", (text or "").lower()) if w not in STOP}


def _with_mentions(text: str, usernames: list[str], cap: int) -> str:
    tags = " ".join(f"@{u}" for u in usernames[:cap])
    joined = f"{text} {tags}" if tags else text
    return joined[:500]


def status(db: DB) -> dict:
    now = time.time()
    day = now - 86400
    g = lambda sql, *a: db.one(sql, a)[0]
    return {
        "accounts_active": g("SELECT COUNT(*) FROM our_accounts WHERE status='active'"),
        "accounts_total": g("SELECT COUNT(*) FROM our_accounts"),
        "accounts_suspended": g("SELECT COUNT(*) FROM our_accounts WHERE status='suspended'"),
        "posts_24h": g("SELECT COUNT(*) FROM our_posts WHERE created_at>?", day),
        "npc_replies_to_us": g("SELECT COALESCE(SUM(npc_replies),0) FROM our_posts"),
        "npc_likes_on_us": g("SELECT COALESCE(SUM(npc_likes),0) FROM our_posts"),
        "our_posts_hidden": g("SELECT COUNT(*) FROM our_posts WHERE status='hidden'"),
        "reports_pending": g("SELECT COUNT(*) FROM reports WHERE status='pending'"),
        "reports_inauthentic": g("SELECT COUNT(*) FROM reports WHERE status='inauthentic'"),
        "reports_genuine": g("SELECT COUNT(*) FROM reports WHERE status='genuine'"),
        "npcs_known": g("SELECT COUNT(*) FROM accounts WHERE npc_score>=0.7"),
        "bots_known": g("SELECT COUNT(*) FROM accounts WHERE npc_score<=0.2"),
        "llm_usd_today": round(db.llm_cost_today(), 3),
    }
