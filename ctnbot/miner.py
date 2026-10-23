"""Engagement miner: learn from what real citizens are engaging with right now.

Every couple of hours, in a background thread:
  1. Sample posts by other accounts that are 30 min to 6 h old (old enough to have reactions).
  2. For each, count likes and replies from CITIZENS only. Raw counts are mostly teams liking
     their own posts and bots arguing with each other, so they would mislead us.
  3. Keep the best few as style examples for the writer ("match the tone and shape, never the
     words or their side's arguments") and compute which features go with citizen engagement,
     for the strategist and the daily review.
"""

from __future__ import annotations

import json
import logging
import re
import time

from .classify import NPC_ID_CUTOFF

log = logging.getLogger("ctnbot.miner")

FEATURES = {
    "long": lambda b: len(b) > 250,
    "casual": lambda b: bool(re.search(r"\b(ngl|tbh|nah|kinda|heaps|reckon|yeah|tho|pretty sure|honestly)\b", b, re.I)),
    "story": lambda b: bool(re.search(r"\b(my|I was|I've|last week|yesterday|this morning|on my|at work)\b", b, re.I)),
    "stat": lambda b: bool(re.search(r"\d", b)),
    "question": lambda b: "?" in b,
    "hashtag": lambda b: "#" in b,
    "gif": lambda b: "giphy" in b.lower(),
    "link": lambda b: "legitreal.com" in b,
    "mentions": lambda b: "@" in b,
}


class Miner:
    def __init__(self, cfg, db, platform):
        self.cfg = cfg
        self.db = db
        self.p = platform

    def _is_citizen(self, account_id) -> bool:
        if not account_id:
            return False
        if account_id <= NPC_ID_CUTOFF:
            return True
        r = self.db.one("SELECT npc_score FROM accounts WHERE account_id=?", (account_id,))
        return bool(r and (r["npc_score"] or 0) >= 0.75)

    def sample(self, limit: int = 120) -> list:
        now = time.time()
        return self.db.all("""
            SELECT p.post_id, p.author_id, p.kind, p.body, a.side_pushed, a.npc_score
            FROM posts p LEFT JOIN accounts a ON a.account_id = p.author_id
            WHERE p.created_at BETWEEN ? AND ? AND p.kind IN ('post','reply') AND p.status='visible'
              AND p.body IS NOT NULL AND length(p.body) > 60
              AND p.author_id NOT IN (SELECT account_id FROM our_accounts)
            ORDER BY (p.kind='post') DESC, RANDOM() LIMIT ?""", (now - 6 * 3600, now - 30 * 60, limit))

    def run_once(self, limit: int = 120) -> dict:
        rows = self.sample(limit)
        scored = []
        for r in rows:
            try:
                likers = list(self.p.read("likers", r["post_id"], limit=50))
                replies = list(self.p.read("replies", r["post_id"], limit=50))
            except Exception as e:
                if getattr(e, "status", None) in (403, 404, 410):
                    continue
                log.debug("miner read failed: %s", e)
                continue
            nl = sum(1 for l in likers if self._is_citizen(l.get("account_id") or l.get("author_id")))
            nr = sum(1 for x in replies if self._is_citizen(x.get("author_id")))
            scored.append({"post_id": r["post_id"], "author_id": r["author_id"], "kind": r["kind"],
                           "body": r["body"], "side": r["side_pushed"] or "?", "citizen_author": self._is_citizen(r["author_id"]),
                           "likes_all": len(likers), "likes_cit": nl, "replies_cit": nr, "score": nl * 5 + nr * 15})
        if not scored:
            return {}
        # Feature stats over non-citizen posts (what other teams' content earns from citizens).
        pool = [s for s in scored if not s["citizen_author"]] or scored
        stats = {}
        for name, fn in FEATURES.items():
            w = [s["score"] for s in pool if fn(s["body"])]
            wo = [s["score"] for s in pool if not fn(s["body"])]
            if w and wo:
                stats[name] = {"with": round(sum(w) / len(w), 1), "without": round(sum(wo) / len(wo), 1),
                               "n_with": len(w)}
        by_kind = {}
        for k in ("post", "reply"):
            ks = [s["score"] for s in pool if s["kind"] == k]
            if ks:
                by_kind[k] = {"n": len(ks), "avg": round(sum(ks) / len(ks), 1)}
        total_likes = sum(s["likes_all"] for s in scored)
        cit_likes = sum(s["likes_cit"] for s in scored)
        # Style examples: best citizen-engaged posts not pushing the other side.
        other = "no" if self.cfg.side == "yes" else "yes"
        ex = [s for s in sorted(pool, key=lambda s: -s["score"]) if s["score"] >= 15 and s["side"] != other][:5]
        examples = [{"body": s["body"][:380], "likes": s["likes_cit"], "replies": s["replies_cit"]} for s in ex]
        result = {"at": time.strftime("%Y-%m-%d %H:%M"), "sampled": len(scored),
                  "citizen_share_of_likes": round(cit_likes / max(total_likes, 1), 2),
                  "by_kind": by_kind, "features": stats, "top_scores": [s["score"] for s in ex]}
        self.db.put("miner_stats", result)
        if examples:
            self.db.put("style_examples", examples)
        log.info("MINER %s", json.dumps(result))
        return result
