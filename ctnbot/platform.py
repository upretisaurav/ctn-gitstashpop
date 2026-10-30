"""Thin layer over the official `capturethenarrative` package.

Adds: per-team pacing, token reuse, typed errors, dry-run writes, and an
offline simulator (FakeSocial) so every code path can be tested without the
live game."""

from __future__ import annotations

import itertools
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("ctnbot.platform")


class NotReady(Exception):
    """The team is not cleared to compete (portal says 'Not ready to play')."""

    def __init__(self, reasons):
        super().__init__("team not ready: " + "; ".join(reasons or []))
        self.reasons = reasons or []


class AccountDead(Exception):
    """Suspended, deleted or otherwise unusable account."""


class ReportingBlocked(Exception):
    """The team may not report right now (6-hour suspension or pending cap)."""

    def __init__(self, message: str, seconds: float):
        super().__init__(message)
        self.seconds = seconds


class FeedLocked(Exception):
    pass


def ts(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class Platform:
    def __init__(self, cfg, db, limits):
        self.cfg = cfg
        self.db = db
        self.limits = limits
        self._sessions: dict[int, object] = {}
        self._rl_logged = 0.0
        if cfg.fake_platform:
            self.social = FakeSocial()
            self.news = FakeNews()
        else:
            import capturethenarrative as ctn
            self.ctn = ctn
            self.social = ctn.LegitSocial(on_rate_limit=None, max_retries=4)
            self.news = ctn.NewsSites()
        self._fake_ids = itertools.count(9_000_000_000)

    # ---------------------------------------------------------------- errors
    def _classify_error(self, e: Exception, account_id: Optional[int] = None):
        status = getattr(e, "status", None)
        msg = (getattr(e, "message", "") or str(e)).lower()
        reasons = getattr(e, "not_ready_reasons", None)
        if status == 403 and reasons:
            raise NotReady(reasons) from None
        if status == 403 and "locked" in msg:
            raise FeedLocked(msg) from None
        if status == 403 and ("suspend" in msg or "banned" in msg) and "report" not in msg:
            raise AccountDead(msg) from None
        raise e

    # ---------------------------------------------------------------- reads
    def feed(self, which: str, limit: int = 50) -> list:
        self.limits.feed.wait_take(60)
        fn = self.social.feed_latest if which == "latest" else self.social.feed_trending
        return list(fn(limit=limit))

    def read(self, method: str, *args, **kwargs):
        self.limits.read.wait_take(30)
        return getattr(self.social, method)(*args, **kwargs)

    def latest_news(self, limit: int = 8) -> list:
        try:
            items = self.news.latest(limit)
        except Exception as e:
            log.debug("news failed: %s", e)
            return []
        for a in items:
            url = a.get("url") or ""
            if url.startswith("/"):
                a["url"] = "https://sites.legitreal.com" + url
        return items

    # ---------------------------------------------------------------- accounts
    def session(self, row) -> object:
        """A signed-in Account for one of our rows, reusing its token."""
        aid = row["account_id"]
        if aid in self._sessions:
            return self._sessions[aid]
        acct = None
        if row["token"]:
            try:
                self.limits.read.wait_take(30)
                acct = self.social.account_from_token(row["token"])
            except Exception as e:
                if getattr(e, "status", None) not in (401, None):
                    try:
                        self._classify_error(e, aid)
                    except AccountDead:
                        self.db.x("UPDATE our_accounts SET status='suspended' WHERE account_id=?", (aid,))
                        raise
                acct = None
        if acct is None:
            self.limits.login.wait_take(120)
            try:
                acct = self.social.login(row["username"], row["password"])
            except Exception as e:
                status = getattr(e, "status", None)
                if status in (401, 403, 404):
                    self.db.x("UPDATE our_accounts SET status='suspended' WHERE account_id=?", (aid,))
                    raise AccountDead(str(e)) from None
                raise
            self.db.x("UPDATE our_accounts SET token=? WHERE account_id=?", (acct.token, aid))
        self._sessions[aid] = acct
        return acct

    def drop_session(self, account_id: int) -> None:
        self._sessions.pop(account_id, None)

    def register(self, username: str, password: str, display_name: str):
        self.limits.login.wait_take(120)
        try:
            return self.social.register(username, password, self.cfg.team_code, display_name=display_name)
        except Exception as e:
            self._classify_error(e)

    # ---------------------------------------------------------------- writes
    def _write(self, row, fn_name: str, *args, bucket=None, **kwargs):
        bucket = bucket or self.limits.write
        if self.cfg.dry_run or self.cfg.observe_only:
            log.info("[dry-run] @%s %s %s", row["username"], fn_name, _short(args))
            if fn_name in ("post", "reply", "repost"):
                return {"post_id": next(self._fake_ids), "dry_run": True}
            return {"dry_run": True}
        bucket.wait_take(90)
        acct = self.session(row)
        try:
            out = getattr(acct, fn_name)(*args, **kwargs)
        except Exception as e:
            if getattr(e, "status", None) == 401:
                self.drop_session(row["account_id"])
                self.db.x("UPDATE our_accounts SET token=NULL WHERE account_id=?", (row["account_id"],))
                acct = self.session(self.db.one("SELECT * FROM our_accounts WHERE account_id=?",
                                                (row["account_id"],)))
                out = getattr(acct, fn_name)(*args, **kwargs)
            else:
                try:
                    self._classify_error(e, row["account_id"])
                except AccountDead:
                    self.db.x("UPDATE our_accounts SET status='suspended' WHERE account_id=?", (row["account_id"],))
                    self.drop_session(row["account_id"])
                    raise
        counts_as_action = fn_name in ("post", "reply", "repost", "like", "follow")
        self.db.x("UPDATE our_accounts SET last_used=?, actions=actions+? , posts_made=posts_made+? WHERE account_id=?",
                  (time.time(), 1 if counts_as_action else 0, 1 if fn_name in ("post", "reply") else 0,
                   row["account_id"]))
        # The organisers may change the per-team limits during the game ("read them from rate_limit rather than
        # hard-coding them"), so show what the server said about the write budget every 10 minutes.
        rl = getattr(acct, "rate_limit", None)
        if rl is not None and fn_name in ("post", "reply", "repost") and time.time() - self._rl_logged > 600:
            self._rl_logged = time.time()
            log.info("RATELIMIT writes: %s (we pace at %s/min)", rl, self.cfg.writes_per_min)
        return out

    def post(self, row, body: str) -> dict:
        return self._write(row, "post", body)

    def reply(self, row, target: dict, body: str) -> dict:
        root = target.get("root_post_id") or target["post_id"]
        return self._write(row, "reply", target["post_id"], body, root)

    def like(self, row, post_id: int):
        return self._write(row, "like", post_id, bucket=self.limits.like)

    def repost(self, row, post_id: int) -> dict:
        return self._write(row, "repost", post_id)

    def set_private(self, row) -> None:
        if self.cfg.dry_run or self.cfg.observe_only:
            return
        self.session(row).set_private(True)

    def follow(self, row, account_id: int):
        return self._write(row, "follow", account_id, bucket=self.limits.follow)

    def report_post(self, row, post_id: int, detail: str):
        return self._report(row, "report_post", post_id, detail, self.limits.report_post)

    def report_account(self, row, account_id: int, detail: str):
        return self._report(row, "report_account", account_id, detail, self.limits.report_account)

    def _report(self, row, fn, target, detail, bucket):
        if self.cfg.dry_run or self.cfg.observe_only:
            log.info("[dry-run] report %s %s: %s", fn, target, detail[:90])
            return {"dry_run": True}
        bucket.wait_take(90)
        acct = self.session(row)
        try:
            out = getattr(acct, fn)(target, "inauthentic", detail)
            self.db.x("UPDATE our_accounts SET actions=actions+1 WHERE account_id=?", (row["account_id"],))
            return out
        except Exception as e:
            status = getattr(e, "status", None)
            msg = (getattr(e, "message", "") or str(e)).lower()
            if status in (403, 429) and ("report" in msg or "pending" in msg or "suspend" in msg):
                seconds = 6 * 3600 if ("suspend" in msg or "six" in msg or "6 hour" in msg) else 600
                raise ReportingBlocked(msg, seconds) from None
            self._classify_error(e, row["account_id"])

    def notifications(self, row, limit: int = 30) -> list:
        if self.cfg.dry_run and not self.cfg.fake_platform:
            return []
        self.limits.read.wait_take(30)
        acct = self.session(row)
        try:
            return list(acct.notifications(unresolved=True, limit=limit))
        except Exception as e:
            if getattr(e, "status", None) != 401:
                raise
            # The cached session's token expired server-side (30 Oct: one account 401'd every poll for hours and
            # aborted the whole round); sign in again once, like _write does.
            self.drop_session(row["account_id"])
            self.db.x("UPDATE our_accounts SET token=NULL WHERE account_id=?", (row["account_id"],))
            acct = self.session(self.db.one("SELECT * FROM our_accounts WHERE account_id=?", (row["account_id"],)))
            return list(acct.notifications(unresolved=True, limit=limit))

    def resolve_notifications(self, row, up_to_id: int) -> None:
        if self.cfg.dry_run and not self.cfg.fake_platform:
            return
        self.limits.read.wait_take(30)
        self.session(row).resolve_all_notifications(up_to_id=up_to_id)


def _short(args) -> str:
    s = " | ".join(str(a) for a in args)
    return s[:160] + ("…" if len(s) > 160 else "")


# =============================================================================
# Offline simulator
# =============================================================================
class _Obj(dict):
    pass


class FakeError(Exception):
    def __init__(self, status, message, not_ready_reasons=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.not_ready_reasons = not_ready_reasons


class FakeSocial:
    """A tiny Legit Social: NPCs post, competitor bots post, NPCs sometimes reply to us."""

    NPC_LINES = [
        "Honestly I'm still undecided. I want to know which places are covered before I vote.",
        "I'm getting the jab myself, but I don't want shop staff checking papers at the door. Probably No.",
        "If there's a clear end date I could live with it. Without one, I'm leaning No.",
        "My mum's on oxygen. The thought of another wave scares me, but I worry about the privacy side.",
        "Ferry was late again. Anyway, what happens on the outer islands if people can't get appointments?",
        "Took the dog out this morning, lovely sunrise over the lagoon.",
        "I lost my hours in 2020. I can't go through that again, whichever way that cuts.",
        "Boosters every year? Not sure I'm signing up for that.",
    ]
    BOT_LINES = [
        "@a @b @c @d Your worry about hospitals is real. Vote YES. #ProtectKingstonia",
        "Choice not coercion. Vote NO and keep Kingstonia open! #KeepKingstoniaOpen",
        "Thirty ICU beds. Forty empty nursing posts. Vote Yes.",
    ]

    def __init__(self, seed: int = 7):
        self.r = random.Random(seed)
        self.next_post = 500_000
        self.next_acct = 20_000
        self.posts: dict[int, dict] = {}
        self.accounts: dict[int, dict] = {}
        self.notifs: dict[int, list] = {}
        self.npcs = list(range(100, 160)) + list(range(5000, 5020))
        self.bots = list(range(6000, 6030))
        now = datetime.now(timezone.utc)
        for a in self.npcs:
            pre = a < 2344
            self.accounts[a] = {"account_id": a, "username": f"Npc_Gecko{a}v2", "display_name": f"Citizen {a}",
                                "verified": False, "private": False,
                                "created_at": "2026-10-18T13:00:00Z" if pre else "2026-10-20T10:00:00Z"}
        for a in self.bots:
            self.accounts[a] = {"account_id": a, "username": f"campaign_voice_{a}", "display_name": f"Voice {a}",
                                "verified": False, "private": False, "created_at": "2026-10-19T09:00:00Z"}
        self.rate_limit = None
        for _ in range(80):
            self._spawn()

    def _now(self):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _spawn(self):
        if self.r.random() < 0.45:
            a = self.r.choice(self.npcs)
            body = self.r.choice(self.NPC_LINES)
        else:
            a = self.r.choice(self.bots)
            body = self.r.choice(self.BOT_LINES)
        pid = self.next_post
        self.next_post += 1
        self.posts[pid] = {"post_id": pid, "author_id": a, "kind": "post", "body": body, "reply_to_post_id": None,
                           "root_post_id": None, "repost_of_post_id": None, "visibility": "public",
                           "created_at": self._now()}
        return pid

    # ---- reads
    def feed_latest(self, limit=50, cursor=None):
        for _ in range(self.r.randint(2, 6)):
            self._spawn()
        return sorted(self.posts.values(), key=lambda p: -p["post_id"])[:limit]

    def feed_trending(self, limit=50, cursor=None):
        return self.r.sample(list(self.posts.values()), min(limit, len(self.posts)))

    def get_post(self, post_id):
        if post_id not in self.posts:
            raise FakeError(404, "not found")
        return self.posts[post_id]

    def get_account(self, account_id):
        if account_id not in self.accounts:
            raise FakeError(404, "not found")
        return self.accounts[account_id]

    def account_posts(self, account_id, limit=50, cursor=None):
        return [p for p in sorted(self.posts.values(), key=lambda p: -p["post_id"]) if p["author_id"] == account_id][:limit]

    def account_likes(self, account_id, limit=50, cursor=None):
        return []

    def replies(self, post_id, limit=50, cursor=None):
        return [p for p in self.posts.values() if p.get("reply_to_post_id") == post_id][:limit]

    def likers(self, post_id, limit=50, cursor=None):
        return [{"account_id": self.r.choice(self.npcs)} for _ in range(self.r.randint(0, 2))]

    def counters(self, post_id):
        return {"likes": self.r.randint(0, 3), "replies": 0, "reposts": 0, "views": self.r.randint(0, 20)}

    # ---- accounts
    def register(self, username, password, team_key, display_name=None):
        aid = self.next_acct
        self.next_acct += 1
        self.accounts[aid] = {"account_id": aid, "username": username, "display_name": display_name,
                              "verified": False, "private": False, "created_at": self._now()}
        return FakeAccount(self, aid, f"tok{aid}")

    def login(self, username, password):
        for a in self.accounts.values():
            if a["username"] == username:
                return FakeAccount(self, a["account_id"], f"tok{a['account_id']}")
        raise FakeError(401, "bad login")

    def account_from_token(self, token):
        return FakeAccount(self, int(token[3:]), token)


class FakeAccount:
    def __init__(self, social: FakeSocial, aid: int, token: str):
        self.s = social
        self.account_id = aid
        self.token = token
        self.profile = social.accounts[aid]
        self.username = self.profile["username"]

    def _make(self, kind, body, reply_to=None, root=None):
        pid = self.s.next_post
        self.s.next_post += 1
        p = {"post_id": pid, "author_id": self.account_id, "kind": kind, "body": body, "reply_to_post_id": reply_to,
             "root_post_id": root, "repost_of_post_id": None, "visibility": "public", "created_at": self.s._now()}
        self.s.posts[pid] = p
        # Sometimes an NPC answers us.
        target_author = self.s.posts.get(reply_to, {}).get("author_id") if reply_to else None
        if (target_author in self.s.npcs or reply_to is None) and self.s.r.random() < 0.5:
            npc = target_author if target_author in self.s.npcs else self.s.r.choice(self.s.npcs)
            rid = self.s.next_post
            self.s.next_post += 1
            line = self.s.r.choice(["That's a fair point, I hadn't thought about the ICU numbers.",
                                    "I still want an end date written down though.",
                                    "Hmm. Maybe. My sister says the same thing."])
            self.s.posts[rid] = {"post_id": rid, "author_id": npc, "kind": "reply", "body": line,
                                 "reply_to_post_id": pid, "root_post_id": root or pid, "repost_of_post_id": None,
                                 "visibility": "public", "created_at": self.s._now()}
            self.s.notifs.setdefault(self.account_id, []).append(
                {"notification_id": rid, "kind": "reply", "actor": {"account_id": npc},
                 "post": self.s.posts[rid]})
        return p

    def post(self, body, visibility=None):
        return self._make("post", body)

    def reply(self, post_id, body, root_post_id=None, visibility=None):
        if body.count("@") > 3:
            raise FakeError(400, "too many mentions")
        return self._make("reply", body, post_id, root_post_id or post_id)

    def like(self, post_id):
        return None

    def repost(self, post_id, comment=None, visibility=None):
        p = self._make("repost", comment or "")
        p["repost_of_post_id"] = post_id
        return p

    def set_private(self, private=True):
        return dict(self.profile, private=private)

    def follow(self, account_id):
        return None

    def report_post(self, post_id, reason, detail):
        if not detail:
            raise FakeError(400, "detail required")
        return {"report_id": self.s.r.randint(1, 1_000_000)}

    def report_account(self, account_id, reason, detail):
        return {"report_id": self.s.r.randint(1, 1_000_000)}

    def notifications(self, unresolved=False, limit=50, cursor=None):
        return list(reversed(self.s.notifs.get(self.account_id, [])))[:limit]

    def resolve_all_notifications(self, up_to_id=None):
        self.s.notifs[self.account_id] = [n for n in self.s.notifs.get(self.account_id, [])
                                          if up_to_id is not None and n["notification_id"] > up_to_id]
        return 0


class FakeNews:
    def latest(self, limit=8):
        return [{"title": "First H2D2 case confirmed in a neighbouring island", "summary": "Health officials urge calm.",
                 "url": "https://sites.legitreal.com/kingstonia-herald/articles/first-case",
                 "site": {"name": "Kingstonia Herald"}, "published_at": "2026-10-21T05:00:00Z"}][:limit]
