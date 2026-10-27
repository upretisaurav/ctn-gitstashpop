"""Measure what works and lean into it.

Every post we make is tagged with its persona and argument. After it has had
time to land, we count NPC replies, likes and reposts, and ask Haiku whether
NPC replies show them moving. That reward feeds a UCB bandit that picks the
next argument (and persona, among those that fit the target)."""

from __future__ import annotations

import json
import logging
import math
import random
import time

from .knowledge import ARGUMENTS, PERSONA_FIT

log = logging.getLogger("ctnbot.learn")


class Bandit:
    def __init__(self, db, c: float = 0.6):
        self.db = db
        self.c = c

    def _stats(self, arms):
        rows = {r["arm"]: r for r in self.db.all(
            f"SELECT * FROM bandit WHERE arm IN ({','.join('?' * len(arms))})", arms)} if arms else {}
        return rows

    def choose(self, prefix: str, options: list[str], prefer: list[str] | None = None,
               weights: dict | None = None) -> str:
        arms = [f"{prefix}:{o}" for o in options]
        st = self._stats(arms)
        total = sum(st[a]["pulls"] for a in arms if a in st) + 1
        best, best_score = None, -1e9
        for o, a in zip(options, arms):
            r = st.get(a)
            pulls = (r["pulls"] if r else 0) + 1.0
            mean = ((r["reward"] if r else 0) + 0.5) / pulls       # optimistic prior of 0.5
            bonus = self.c * math.sqrt(math.log(total + 1) / pulls)
            fit = 0.25 if prefer and o in prefer else 0.0
            w = float((weights or {}).get(o, 1.0))
            score = (mean + bonus + fit) * w + random.random() * 0.05
            if score > best_score:
                best, best_score = o, score
        return best

    def best(self, prefix: str, options: list[str], min_pulls: int = 50) -> str | None:
        """Option with the highest average reward so far (ignoring arms with too few pulls); None if no data."""
        st = self._stats([f"{prefix}:{o}" for o in options])
        scored = [(st[f"{prefix}:{o}"]["reward"] / st[f"{prefix}:{o}"]["pulls"], o) for o in options
                  if f"{prefix}:{o}" in st and st[f"{prefix}:{o}"]["pulls"] >= min_pulls]
        return max(scored)[1] if scored else None

    def pull(self, prefix: str, option: str) -> None:
        self.db.x("INSERT INTO bandit(arm,pulls,reward) VALUES(?,1,0) ON CONFLICT(arm) DO UPDATE SET pulls=pulls+1",
                  (f"{prefix}:{option}",))

    def reward(self, prefix: str, option: str, r: float) -> None:
        self.db.x("INSERT INTO bandit(arm,pulls,reward) VALUES(?,0,?) ON CONFLICT(arm) DO UPDATE SET reward=reward+?",
                  (f"{prefix}:{option}", r, r))


def arguments_for(concerns: list[str]) -> list[str]:
    fits = [k for k, v in ARGUMENTS.items() if any(c in v["for"] for c in concerns)]
    return fits or list(ARGUMENTS)


def personas_for(concerns: list[str], available: list[str]) -> list[str]:
    fits = [p for p in available if any(c in PERSONA_FIT.get(p, []) for c in concerns)]
    return fits or available


class EngagementTracker:
    """Looks back at our posts (once at ~25 min, again at ~3 h) and scores them."""

    def __init__(self, cfg, db, platform, writer, bandit):
        self.cfg = cfg
        self.db = db
        self.p = platform
        self.writer = writer
        self.bandit = bandit

    def run_once(self, limit: int = 6) -> None:
        # Newest first: fresh 25-min checks feed the learner while any older backlog drains behind them.
        now = time.time()
        # Citizens answer from a backlog: 72% of their replies (27 Oct sample) went to posts older than 6 h and
        # 40% to posts older than a day, so a third look at ~22 h catches most of what the 3 h check missed.
        rows = self.db.all("""SELECT * FROM our_posts WHERE status='visible' AND post_id < 9000000000 AND (
                                (checked_at = 0 AND created_at < ?) OR
                                (checked_at > 0 AND checked_at < created_at + 3600 AND created_at < ?) OR
                                (checked_at > 0 AND checked_at < created_at + 20 * 3600 AND created_at < ?))
                              ORDER BY created_at DESC LIMIT ?""",
                           (now - 25 * 60, now - 3 * 3600, now - 22 * 3600, limit))
        for row in rows:
            try:
                self._score(row)
            except Exception as e:
                status = getattr(e, "status", None)
                if status in (404, 410, 403):
                    self.db.x("UPDATE our_posts SET status='hidden', checked_at=? WHERE post_id=?", (now, row["post_id"]))
                    log.warning("our post %s is gone/hidden (status %s) — account %s may be under report",
                                row["post_id"], status, row["account_id"])
                    self.bandit.reward("persona", row["persona_key"], -1.0)
                else:
                    log.debug("engagement check failed: %s", e)
                    self.db.x("UPDATE our_posts SET checked_at=? WHERE post_id=?", (now, row["post_id"]))

    # Only simulated citizens score points, and they are exactly account ids <= 2343 (every later id is a team
    # account). The classifier rates ~3,400 rival-team bots as NPCs (npc_score >= 0.6), so counting by npc_score
    # filled rewards, shift judgements and REPORT with No-side bots answering us. Count citizens only.
    CITIZEN_MAX_ID = 2343
    # Posts first scored before this cutoff stored counts that included rival bots; re-scoring them would hand the
    # bandits a spurious negative delta, so their second check updates the row but skips the bandit reward.
    CITIZEN_ONLY_SINCE = 1792998000.0       # 2026-10-26 07:00 UTC (deploy of the citizen-only learner)

    def _is_npc(self, account_id: int) -> bool:
        return 0 < (account_id or 0) <= self.CITIZEN_MAX_ID

    def _score(self, row) -> None:
        pid = row["post_id"]
        self.p.read("get_post", pid)               # raises if hidden/gone
        likers = list(self.p.read("likers", pid, limit=50))
        replies = list(self.p.read("replies", pid, limit=30))
        npc_likes = sum(1 for l in likers if self._is_npc(l.get("account_id") or l.get("author_id") or 0))
        npc_reps = [r for r in replies if self._is_npc(r.get("author_id", 0))]
        shift_total = 0.0
        for r in npc_reps[:3]:
            shift, moved = self.writer.judge_shift(row["body"] or "", r.get("body") or "")
            shift_total += shift
            if moved:
                self.db.x("UPDATE accounts SET moved_until=? WHERE account_id=?", (time.time() + 2 * 3600, r["author_id"]))
        reward = len(npc_reps) * 1.0 + npc_likes * 0.3 + max(0.0, shift_total) * 3 - max(0.0, -shift_total) * 2
        reward = max(-2.0, min(6.0, reward))
        prev = self.db.one("SELECT npc_likes,npc_replies,shift_score,checked_at FROM our_posts WHERE post_id=?", (pid,))
        stale = 0 < (prev["checked_at"] or 0) < self.CITIZEN_ONLY_SINCE     # first check counted rival bots
        delta = reward - ((prev["npc_replies"] or 0) * 1.0 + (prev["npc_likes"] or 0) * 0.3 +
                          max(0.0, prev["shift_score"] or 0) * 3)
        sample = " | ".join((r.get("body") or "")[:200] for r in npc_reps[:2])
        self.db.x("""UPDATE our_posts SET npc_likes=?, npc_replies=?, shift_score=?, checked_at=?,
                     npc_reply_sample=COALESCE(NULLIF(?, ''), npc_reply_sample) WHERE post_id=?""",
                  (npc_likes, len(npc_reps), shift_total, time.time(), sample, pid))
        if stale:
            delta = 0.0
        if row["argument"]:
            self.bandit.reward("arg", row["argument"], delta)
        self.bandit.reward("persona", row["persona_key"], delta)
        self.bandit.reward("kind", row["kind"], delta)
        self.bandit.reward("gif", "yes" if "giphy" in (row["body"] or "") else "no", delta)
        cit_reps = sum(1 for r in replies if (r.get("author_id") or 10**9) <= 2343)
        cit_likes = sum(1 for l in likers if (l.get("account_id") or l.get("author_id") or 10**9) <= 2343)
        log.info("post %s (%s/%s): %d NPC replies, %d NPC likes, shift %.2f (citizens: %d replies, %d likes)", pid,
                 row["persona_key"], row["argument"], len(npc_reps), npc_likes, shift_total, cit_reps, cit_likes)
