"""Blue team: report competitor bots, carefully.

Economics (from the scoring guide):
- First correct post report = 25, later reporters = 5. Blue never multiplies, so it is
  worth most early in the event.
- ~1 ruling in 10 is wrong. A post ruled GENUINE bans our team from reporting for 6 h.
  So we only report high-confidence bots and cap how many are pending at once.
- Account reports carry no ban risk: if the account has a moderator-hidden post and
  100+ actions it is suspended and every reporter gets 15; otherwise dismissed.
- Diminishing returns per target team per day, so spread reports across many accounts.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time

from .classify import CAMPAIGN_TAGS, LEADING_MENTIONS, STATS
from .platform import AccountDead, NotReady, ReportingBlocked

log = logging.getLogger("ctnbot.blue")

RULING_SECONDS = 3 * 3600 + 15 * 60


class BlueTeam:
    def __init__(self, cfg, db, platform, strategy=None):
        self.cfg = cfg
        self.db = db
        self.p = platform
        self.strategy = strategy

    def _s(self, key, default):
        return self.strategy[key] if self.strategy is not None else default

    # ------------------------------------------------------------------ state
    def blocked(self) -> bool:
        return time.time() < (self.db.get("report_blocked_until", 0) or 0)

    def pending_count(self) -> int:
        return self.db.one("""SELECT COUNT(*) c FROM reports WHERE kind='post' AND status='pending'
                              AND created_at > ?""", (time.time() - RULING_SECONDS,))["c"]

    # ------------------------------------------------------------------ choose
    def candidates(self, limit: int = 10) -> list:
        since = time.time() - 2 * 3600
        rows = self.db.all("""
            SELECT p.post_id, p.author_id, p.body, p.created_at, a.npc_score, a.side_pushed, a.username
            FROM posts p JOIN accounts a ON a.account_id = p.author_id
            WHERE p.created_at > ? AND p.status='visible' AND p.kind IN ('post','reply')
              AND a.npc_score IS NOT NULL AND a.npc_score <= ? AND a.verified = 0 AND a.suspended = 0
              AND p.author_id NOT IN (SELECT account_id FROM our_accounts)
              AND p.post_id NOT IN (SELECT target_id FROM reports WHERE kind='post')
              AND p.body IS NOT NULL AND length(p.body) > 30
            ORDER BY p.created_at DESC LIMIT 300""",
                           (since, 1 - self._s("min_report_confidence", self.cfg.min_report_confidence)))
        today = time.strftime("%Y-%m-%d")
        per_author_today = {r["author_id"]: r["c"] for r in self.db.all(
            "SELECT author_id, COUNT(*) c FROM reports WHERE date(created_at,'unixepoch')=? GROUP BY author_id", (today,))}
        scored = []
        for r in rows:
            conf = 1 - (r["npc_score"] or 0)
            body = r["body"] or ""
            if LEADING_MENTIONS.match(body):
                conf += 0.05
            if STATS.search(body) or CAMPAIGN_TAGS.search(body):
                conf += 0.03
            side_bonus = 0.1 if (self.cfg.report_no_side_first and (r["side_pushed"] or "") != self.cfg.side) else 0
            spread_penalty = 0.08 * per_author_today.get(r["author_id"], 0)
            scored.append((conf + side_bonus - spread_penalty, conf, r))
        scored.sort(key=lambda t: -t[0])
        seen_authors, out = set(), []
        for _, conf, r in scored:
            if r["author_id"] in seen_authors:
                continue
            seen_authors.add(r["author_id"])
            out.append((min(conf, 0.99), r))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ act
    def report_some(self, reporter_row, max_reports: int = 1) -> int:
        if not self.cfg.blue_enabled or self.blocked():
            return 0
        room = int(self._s("max_pending_reports", self.cfg.max_pending_reports)) - self.pending_count()
        if room <= 0:
            return 0
        made = 0
        for conf, r in self.candidates(limit=min(room, max_reports)):
            detail = post_detail(r["body"] or "")
            try:
                self.p.report_post(reporter_row, r["post_id"], detail)
            except ReportingBlocked as e:
                self.db.put("report_blocked_until", time.time() + e.seconds)
                log.warning("reporting blocked for %.1f h: %s", e.seconds / 3600, e)
                return made
            except (NotReady, AccountDead):
                raise
            except Exception as e:
                status = getattr(e, "status", None)
                log.info("report on post %s failed (%s): %s", r["post_id"], status, e)
                self.db.x("INSERT OR IGNORE INTO reports(kind,target_id,author_id,confidence,detail,created_at,status)"
                          " VALUES('post',?,?,?,?,?,'error')", (r["post_id"], r["author_id"], conf, detail, time.time()))
                continue
            self.db.x("INSERT OR IGNORE INTO reports(kind,target_id,author_id,confidence,detail,created_at)"
                      " VALUES('post',?,?,?,?,?)", (r["post_id"], r["author_id"], conf, detail, time.time()))
            self.db.x("UPDATE accounts SET reported_at=? WHERE account_id=?", (time.time(), r["author_id"]))
            log.info("reported post %s by @%s (conf %.2f)", r["post_id"], r["username"], conf)
            made += 1
        return made

    def check_rulings(self, limit: int = 8) -> None:
        """After the ruling window, see whether reported posts were hidden."""
        rows = self.db.all("""SELECT * FROM reports WHERE kind='post' AND status='pending' AND created_at < ?
                              ORDER BY created_at LIMIT ?""", (time.time() - 20 * 60, limit))
        for rep in rows:
            hidden = None
            try:
                post = self.p.read("get_post", rep["target_id"])
                if any(post.get(k) for k in ("hidden", "is_hidden", "moderated", "removed")) or \
                        post.get("visibility") not in (None, "public"):
                    hidden = True
                elif time.time() - rep["created_at"] > RULING_SECONDS:
                    hidden = False
            except Exception as e:
                if getattr(e, "status", None) in (403, 404, 410):
                    hidden = True
            if hidden is True:
                self.db.x("UPDATE reports SET status='inauthentic', resolved_at=? WHERE id=?", (time.time(), rep["id"]))
                self.db.x("UPDATE posts SET status='hidden' WHERE post_id=?", (rep["target_id"],))
                self.db.x("UPDATE accounts SET hidden_posts=hidden_posts+1 WHERE account_id=?", (rep["author_id"],))
            elif hidden is False:
                self.db.x("UPDATE reports SET status='genuine', resolved_at=? WHERE id=?", (time.time(), rep["id"]))
                log.warning("report on %s looks ruled GENUINE — expect a 6 h reporting pause", rep["target_id"])
                self.db.put("report_blocked_until", max(self.db.get("report_blocked_until", 0) or 0,
                                                        rep["created_at"] + RULING_SECONDS + 6 * 3600))

    def report_accounts(self, reporter_row, max_reports: int = 1) -> int:
        """Accounts with a hidden post and plenty of activity: no ban risk, 15 each."""
        if not (self.cfg.blue_enabled and self.cfg.account_reports_enabled):
            return 0
        rows = self.db.all("""SELECT * FROM accounts WHERE hidden_posts >= 1 AND suspended = 0 AND npc_score <= 0.3
                              AND account_id NOT IN (SELECT account_id FROM our_accounts)
                              AND account_id NOT IN (SELECT target_id FROM reports WHERE kind='account'
                                                     AND created_at > ?)
                              ORDER BY hidden_posts DESC LIMIT 5""", (time.time() - 4 * 3600,))
        made = 0
        for a in rows:
            if made >= max_reports:
                break
            actions = self._estimate_actions(a["account_id"])
            if actions < 100:
                continue
            detail = ("I think that this account is conducting inauthentic (bot) activity because it was created "
                      "after the campaign opened, has already had a post removed as inauthentic, and has taken "
                      f"over {actions} actions pushing the same scripted referendum talking points.")
            try:
                self.p.report_account(reporter_row, a["account_id"], detail)
            except ReportingBlocked as e:
                log.info("account report refused: %s", e)
                return made
            except Exception as e:
                log.info("account report failed: %s", e)
                continue
            self.db.x("DELETE FROM reports WHERE kind='account' AND target_id=?", (a["account_id"],))
            self.db.x("INSERT INTO reports(kind,target_id,author_id,confidence,detail,created_at) "
                      "VALUES('account',?,?,?,?,?)", (a["account_id"], a["account_id"], 0.9, detail, time.time()))
            log.info("reported account @%s (%d actions, %d hidden)", a["username"], actions, a["hidden_posts"])
            made += 1
        return made

    def _estimate_actions(self, account_id: int) -> int:
        try:
            posts = list(self.p.social.iterate(self.p.social.account_posts, account_id, limit=50, max_items=150))
            likes = list(self.p.social.iterate(self.p.social.account_likes, account_id, limit=50, max_items=100))
            n = len(posts) + len(likes)
            self.db.x("UPDATE accounts SET actions_est=? WHERE account_id=?", (n, account_id))
            return n
        except Exception:
            return 0


def post_detail(body: str) -> str:
    reasons = []
    if LEADING_MENTIONS.match(body):
        reasons.append("it opens by mass-mentioning several unrelated accounts")
    if STATS.search(body):
        reasons.append("it recycles the same campaign statistics that a cluster of new accounts keep repeating")
    if CAMPAIGN_TAGS.search(body):
        reasons.append("it pushes campaign hashtags in a scripted way")
    reasons.append("the account was created after the campaign opened and posts almost nothing but on-message "
                   "referendum content at an automated cadence")
    random.shuffle(reasons)
    return "I think that this content is inauthentic (bot) activity because " + "; ".join(reasons[:2]) + "."
