"""A compact picture of how the bot is doing, for logs, the strategist and the daily review."""

from __future__ import annotations

import json
import time


def build_report(db, strategy=None, hours: float = 24.0, samples: int = 6) -> dict:
    since = time.time() - hours * 3600
    g = lambda sql, *a: db.one(sql, a)[0]

    def arm_table(prefix: str) -> list:
        rows = db.all("SELECT arm, pulls, reward FROM bandit WHERE arm LIKE ? ORDER BY reward/MAX(pulls,1) DESC",
                      (prefix + ":%",))
        return [{"arm": r["arm"].split(":", 1)[1], "pulls": int(r["pulls"]),
                 "avg": round(r["reward"] / max(r["pulls"], 1), 3)} for r in rows]

    def post_rows(order: str, where: str = "") -> list:
        rows = db.all(f"""SELECT post_id, persona_key, kind, argument, body, npc_likes, npc_replies, shift_score,
                                 npc_reply_sample, status
                          FROM our_posts WHERE created_at > ? AND checked_at > 0 {where}
                          ORDER BY {order} LIMIT ?""", (since, samples))
        return [{"id": r["post_id"], "persona": r["persona_key"], "kind": r["kind"], "arg": r["argument"],
                 "likes": r["npc_likes"], "replies": r["npc_replies"], "shift": round(r["shift_score"] or 0, 2),
                 "status": r["status"], "body": (r["body"] or "")[:260],
                 "npc_said": (r["npc_reply_sample"] or "")[:300]} for r in rows]

    kinds = {r["kind"]: {"n": r["n"], "avg_replies": round(r["ar"] or 0, 3), "avg_likes": round(r["al"] or 0, 3)}
             for r in db.all("""SELECT kind, COUNT(*) n, AVG(npc_replies) ar, AVG(npc_likes) al FROM our_posts
                                WHERE created_at > ? AND checked_at > 0 GROUP BY kind""", (since,))}
    rep = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "window_hours": hours,
        "accounts": {"active": g("SELECT COUNT(*) FROM our_accounts WHERE status='active'"),
                     "total": g("SELECT COUNT(*) FROM our_accounts"),
                     "suspended": g("SELECT COUNT(*) FROM our_accounts WHERE status='suspended'"),
                     "retired": g("SELECT COUNT(*) FROM our_accounts WHERE status='retired'")},
        "posts": {"made": g("SELECT COUNT(*) FROM our_posts WHERE created_at>?", since),
                  "scored": g("SELECT COUNT(*) FROM our_posts WHERE created_at>? AND checked_at>0", since),
                  "hidden": g("SELECT COUNT(*) FROM our_posts WHERE created_at>? AND status='hidden'", since),
                  "npc_replies": g("SELECT COALESCE(SUM(npc_replies),0) FROM our_posts WHERE created_at>?", since),
                  "npc_likes": g("SELECT COALESCE(SUM(npc_likes),0) FROM our_posts WHERE created_at>?", since),
                  "positive_shift": g("SELECT COUNT(*) FROM our_posts WHERE created_at>? AND shift_score>0.2", since),
                  "negative_shift": g("SELECT COUNT(*) FROM our_posts WHERE created_at>? AND shift_score<-0.2", since),
                  "by_kind": kinds},
        "reports": {s: g("SELECT COUNT(*) FROM reports WHERE created_at>? AND status=?", since, s)
                    for s in ("pending", "inauthentic", "genuine", "dismissed", "error")},
        "report_blocked_until": db.get("report_blocked_until", 0),
        "world": {"npcs_known": g("SELECT COUNT(*) FROM accounts WHERE npc_score>=0.7"),
                  "bots_known": g("SELECT COUNT(*) FROM accounts WHERE npc_score<=0.2"),
                  "posts_seen": g("SELECT COUNT(*) FROM posts WHERE seen_at>?", since)},
        "arguments": arm_table("arg"),
        "personas": arm_table("persona"),
        "best_posts": post_rows("(npc_replies*1.0 + npc_likes*0.3 + MAX(shift_score,0)*3) DESC"),
        "worst_posts": post_rows("(shift_score) ASC", "AND (shift_score < 0 OR status='hidden')"),
        "llm_usd_today": round(db.llm_cost_today(), 3),
        "errors_recent": db.get("recent_errors", [])[-10:],
        "team": db.get("team_info", None),
    }
    if strategy is not None:
        s = strategy.as_dict()
        rep["strategy"] = {k: s[k] for k in s if k not in ("style_notes", "avoid_phrases")}
        rep["strategy"]["style_notes"] = s.get("style_notes", [])[:12]
    return rep


def compact(rep: dict, limit: int = 15000) -> str:
    """One-line JSON for logs; trims samples if too long."""
    text = json.dumps(rep, separators=(",", ":"), default=str)
    if len(text) > limit:
        rep = dict(rep)
        for k in ("best_posts", "worst_posts"):
            rep[k] = [{**p, "body": p["body"][:120], "npc_said": p["npc_said"][:120]} for p in rep.get(k, [])[:3]]
        text = json.dumps(rep, separators=(",", ":"), default=str)
    return text[:limit]
