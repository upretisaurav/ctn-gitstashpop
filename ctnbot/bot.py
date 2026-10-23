"""The main loop.

The team gets 3 posts/replies/reposts per minute, shared by every account. Each
slot goes to the best available move, in this order:
  1. Continue a conversation: an NPC just replied to one of our accounts.
  2. Reply to a fresh NPC post (they are probably still in their session).
  3. A top-level post that @mentions NPCs who share a worry (some share of slots).
Meanwhile: read the feeds, classify new authors, report competitor bots, and
score our past posts so the bandit learns which arguments and personas work.
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import random
import re
import time
from typing import Callable, Optional

from .blue import BlueTeam
from .classify import NPC_ID_CUTOFF, Classifier
from .config import Config
from .db import DB
from .knowledge import ARGUMENTS, CONCERNS, PERSONA_BY_KEY, PERSONAS, make_password, make_username
from .learn import Bandit, EngagementTracker, arguments_for, personas_for
from .llm import LLM
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
        self.on_tick: Optional[Callable] = None
        self._last_tick_hook = 0.0

    # ======================================================================
    def run(self, duration: Optional[float] = None) -> None:
        log.info("ctnbot starting: side=%s dry_run=%s fake=%s observe_only=%s", self.cfg.side,
                 self.cfg.dry_run, self.cfg.fake_platform, self.cfg.observe_only)
        tasks: list[tuple[float, Callable]] = [
            (9, self.poll_latest),
            (60, self.poll_trending),
            (15, self.classify_some),
            (12, self.poll_notifications),
            (15, self.ensure_accounts),
            (25, self.blue_tick),
            (180, self.blue.check_rulings),
            (600, self.blue_accounts_tick),
            (120, lambda: self.tracker.run_once(4)),
            (900, self.refresh_news),
            (600, self.status_line),
            (60, self.strategy.maybe_reload),
            (600, self.strategist_tick),
            (7200, self.report_line),
            (1800, self.refresh_team_info),
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
            log.exception("task %s failed: %s", getattr(fn, "__name__", fn), e)

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
    def ensure_accounts(self) -> None:
        if self.cfg.observe_only or self.no_more_accounts or time.time() < self.not_ready_until:
            return
        for row in self.db.our_accounts("active"):
            # An account can only be suspended via an account report once a moderator has hidden one of its
            # posts AND it has 100+ actions. So retire exposed accounts just before 100.
            exposed = self.db.one("SELECT COUNT(*) c FROM our_posts WHERE account_id=? AND status='hidden'",
                                  (row["account_id"],))["c"]
            if (exposed and row["actions"] >= self.cfg.retire_after_actions) or exposed >= 2:
                self.db.x("UPDATE our_accounts SET status='retired' WHERE account_id=?", (row["account_id"],))
                log.warning("retired @%s (%d actions, %d hidden posts)", row["username"], row["actions"], exposed)
        active = len(self.db.our_accounts("active"))
        target = min(int(self.strategy["target_active_accounts"]), self.cfg.max_active_accounts)
        if active >= target or self.db.count_our_accounts() >= self.cfg.max_total_accounts:
            return
        self.create_account()

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

    def pick_account(self, persona_options: Optional[list[str]] = None, exclude: set = frozenset()):
        rows = [r for r in self.db.our_accounts("active") if r["account_id"] not in exclude]
        if not rows:
            return None
        if persona_options:
            keys = sorted({r["persona_key"] for r in rows})
            fit = [k for k in keys if k in persona_options] or keys
            chosen = self.bandit.choose("persona", fit, prefer=persona_options,
                                        weights=self.strategy["persona_weights"])
            rows = [r for r in rows if r["persona_key"] == chosen] or rows
        return min(rows, key=lambda r: r["last_used"])

    # ======================================================================  writing
    def write_slot(self) -> None:
        if not self.can_write() or time.time() < self.write_backoff_until or not self.limits.write.available():
            return
        if not self.db.our_accounts("active"):
            return
        done = (self.do_continuation()
                or (random.random() < self.strategy["share_top_level"] and self.do_top_level())
                or self.do_reply()
                or self.do_top_level())
        if not done:
            self.write_backoff_until = time.time() + 6   # nothing worth saying right now; don't spin

    # ---- 1. conversations ------------------------------------------------
    def do_continuation(self) -> bool:
        now = time.time()
        for nid in list(self.continuations):
            if now - self.continuations[nid]["at"] > 15 * 60:
                del self.continuations[nid]
        while self.continuations:
            nid = max(self.continuations, key=lambda k: self.continuations[k]["at"])
            item = self.continuations.pop(nid)
            actor = item["actor"]
            if not self._is_npc(actor):
                continue
            prof = self._profile(actor)
            if prof and (prof["moved_until"] or 0) > now:
                continue
            row = self.db.one("SELECT * FROM our_accounts WHERE account_id=? AND status='active'", (item["account_id"],))
            if not row:
                continue
            their = item["post"]
            try:
                full = self.p.read("get_post", their["post_id"])
            except Exception:
                full = their
            thread, depth = self.thread_context(full)
            if depth is not None and depth >= self.cfg.max_thread_depth:
                continue
            ours = self.db.one("SELECT body FROM our_posts WHERE post_id=?", (full.get("reply_to_post_id"),))
            text, model = self.writer.continuation(
                persona_key=row["persona_key"], display_name=row["display_name"],
                our_post=(ours["body"] if ours else ""), their_reply=full.get("body") or "",
                target_profile=dict(prof) if prof else None, thread=thread)
            if not text:
                continue
            return self._send_reply(row, full, text, "continuation", None, actor, model)
        return False

    # ---- 2. replies to fresh NPC posts -------------------------------------
    def do_reply(self) -> bool:
        now = time.time()
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
            ORDER BY p.created_at DESC LIMIT 80""",
                           (now - int(self.strategy["reply_fresh_seconds"]), now, now - self.cfg.target_cooldown_minutes * 60,
                            self.cfg.max_thread_depth))
        if not rows:
            return False
        rows = [r for r in rows if r["post_id"] not in self.skip_posts]
        scored = sorted(((self._target_score(r, now), r) for r in rows), key=lambda t: -t[0])
        for score, r in scored[:3]:
            if score <= 0.05:
                break
            target = {"post_id": r["post_id"], "root_post_id": r["root_id"], "body": r["body"],
                      "reply_to_post_id": r["reply_to"], "author_id": r["author_id"]}
            thread, depth = self.thread_context(target)
            if depth is not None and depth >= self.cfg.max_thread_depth:
                self.db.x("UPDATE posts SET depth=? WHERE post_id=?", (depth, r["post_id"]))
                continue
            concerns = json.loads(r["concerns"] or "[]") or ["other"]
            row = self.pick_account(personas_for(concerns, [p["key"] for p in PERSONAS]))
            if row is None:
                return False
            if self._life_post_due(row):
                return self.do_life_post(row)
            arg = self.bandit.choose("arg", list(ARGUMENTS), prefer=arguments_for(concerns),
                                     weights=self.strategy["argument_weights"])
            smart = (r["stance"] in (-1, 0, None)) and \
                (r["persuadable"] or 0.5) >= self.strategy["smart_reply_min_persuadable"]
            text, model = self.writer.reply(
                persona_key=row["persona_key"], display_name=row["display_name"], target_post=target,
                thread=thread, target_profile=dict(r), argument=arg, smart=smart)
            if not text:
                self.skip_posts.add(r["post_id"])
                continue
            extra = self._co_targets(concerns, exclude={r["author_id"]},
                                     n=int(self.strategy["extra_mentions_per_reply"]))
            if extra:
                text = _with_mentions(text, [u for _, u in extra], 3)
            return self._send_reply(row, target, text, "reply", arg, r["author_id"], model,
                                    mentions=[a for a, _ in extra])
        return False

    def _target_score(self, r, now: float) -> float:
        sw = self.strategy["stance_weights"]
        stance_w = float(sw.get(str(r["stance"]), 0.7)) if r["stance"] is not None else 0.7
        concerns = json.loads(r["concerns"] or "[]")
        concern_w = max([self.strategy.weight("concern_weights", c) for c in concerns] or [1.0])
        if r["stance"] == 2 and ((r["stance_yearly"] or 0) < 0 or (r["stance_vacc"] or 0) < 0):
            stance_w = 0.5            # yearly/general vaccination views also pay 160
        age = max(0.0, now - (r["created_at"] or now))
        fresh = math.exp(-age / 150.0)
        depth_w = 1.0 if (r["depth"] in (0, None)) else max(0.3, 1 - 0.1 * r["depth"])
        trending = 1.15 if r["author_id"] in set(self.db.get("trending_authors", []) or []) else 1.0
        on_topic = 1.0 if ON_TOPIC.search(r["body"] or "") else 0.15     # don't hijack footy posts
        return (r["npc_score"] or 0) * (r["persuadable"] if r["persuadable"] is not None else 0.5 + 0.0) * \
            stance_w * concern_w * fresh * depth_w * trending * on_topic + 0.01

    # ---- 3. top-level posts ----------------------------------------------
    def do_top_level(self) -> bool:
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
        concern = max(counts, key=counts.get) if counts else random.choice(list(CONCERNS)[:12])
        mentions = self._co_targets([concern], exclude=set(), n=int(self.strategy["mentions_per_post"]),
                                    window=5 * 60)
        if not mentions and counts:
            return False            # nobody active to aim at; a reply opportunity will come
        row = self.pick_account(personas_for([concern], [p["key"] for p in PERSONAS]))
        if row is None:
            return False
        if self._life_post_due(row):
            return self.do_life_post(row)
        arg = self.bandit.choose("arg", list(ARGUMENTS), prefer=arguments_for([concern]),
                                 weights=self.strategy["argument_weights"])
        relevant = [a for a in self.news
                    if ON_TOPIC.search(f"{a.get('title', '')} {a.get('summary') or a.get('excerpt') or ''}")]
        news = random.choice(relevant) if relevant and random.random() < 0.5 else None
        text, model = self.writer.top_level(persona_key=row["persona_key"], display_name=row["display_name"],
                                            argument=arg, news=news, audience_concern=CONCERNS[concern])
        if not text:
            return False
        if news and news.get("url") and len(text) + len(news["url"]) < 470:
            text = f"{text}\n\n{news['url']}"
        if mentions:
            text = _with_mentions(text, [u for _, u in mentions], 4)
        try:
            out = self.p.post(row, text)
        except AccountDead:
            return False
        self._record(out, row, "post", arg, None, None, text, model, [a for a, _ in mentions])
        for i in range(int(self.strategy["boost_likes_per_post"])):
            self._later(random.uniform(60, 420) * (i + 1), self._boost_like, out.get("post_id"), row["account_id"])
        return True

    def do_life_post(self, row) -> bool:
        text, model = self.writer.life_post(persona_key=row["persona_key"], display_name=row["display_name"])
        if not text:
            return False
        try:
            out = self.p.post(row, text)
        except AccountDead:
            return False
        self._record(out, row, "life", None, None, None, text, model, [])
        return True

    def _life_post_due(self, row) -> bool:
        n = self.cfg.life_post_every
        return n > 0 and row["posts_made"] > 0 and row["posts_made"] % n == n - 1

    def _boost_like(self, post_id, author_account_id) -> None:
        if not post_id or post_id >= 9_000_000_000:
            return
        row = self.pick_account(exclude={author_account_id})
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
        row = self.pick_account()
        if row is not None and self.limits.report_post.available():
            made = self.blue.report_some(row, max_reports=1)
            if not made and time.time() - getattr(self, "_blue_note", 0) > 900:
                self._blue_note = time.time()
                log.info("blue: no report this round (candidates=%d, pending=%d, blocked=%s)",
                         len(self.blue.candidates(limit=50)), self.blue.pending_count(), self.blue.blocked())

    def blue_accounts_tick(self) -> None:
        if self.cfg.observe_only or not self.can_write():
            return
        row = self.pick_account()
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
