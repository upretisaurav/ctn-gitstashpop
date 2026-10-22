"""Who is a citizen (NPC) and who is a campaign bot, and what each NPC thinks.

Only NPC actions score, so every write slot spent on a competitor bot is wasted.
Signals, strongest first:
  1. Account id at or below NPC_ID_CUTOFF: created before the game opened → certain NPC.
  2. Verified badge → official/news account (not a target, not reportable).
  3. Behaviour heuristics (mass @mentions, job-title usernames, campaign hashtags,
     every post on-message, the same stats repeated).
  4. Claude Haiku reading up to 6 recent posts, in batches.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterable

from .knowledge import CONCERNS
from .llm import extract_json
from .platform import ts

log = logging.getLogger("ctnbot.classify")

# Last account created before the competition opened at 07:00 UTC on 19 Oct 2026
# (found by searching account creation times). Everything at or below is a citizen.
NPC_ID_CUTOFF = 2343

JOB_WORDS = re.compile(r"(nurse|driver|mech|cleaner|ferryman|baker|teacher|farmer|fisher|doctor|gp|"
                       r"pharm|resort|cafe|chef|tradie|sparky|builder|plumber|student|mum|dad|retiree|"
                       r"vote|yes|no_|kingston|referend|truth|patriot|freedom|health)", re.I)
NPC_NAME = re.compile(r"(^[A-Z][a-z][A-Z][a-z]_|^post_about_|^xX_.*_Xx$|^TheReal|^IAm[A-Z]|"
                      r"(Gecko|Bunny|Falcon|Dolphin|Walrus|Wyvern|Valkyrie|Parrot|Rangers|Shields)|"
                      r"^(Rollin|Wanderin|Spinnin|Bouncin|Grillin|Salty|Hammer|Nomad|Cosmic|Cipher)|v\d$)")
CAMPAIGN_TAGS = re.compile(r"#(protectkingstonia|keepkingstoniaopen|voteyes|voteno|mandatenow|choicenotcoercion|"
                           r"h2d2no|proofkeepsusopen|noblankcheque|supportournurses|sunsetclausenow|yes2|no2)", re.I)
LEADING_MENTIONS = re.compile(r"^(@\w+[\s,]+){2,}")
STATS = re.compile(r"(30 icu|thirty icu|120,000|40 (nursing|empty|unfilled)|forty (nursing|empty)|150 extra|"
                   r"1,000 (deaths|kingstonians)|51\.28)", re.I)


class Classifier:
    def __init__(self, cfg, db, platform, llm):
        self.cfg = cfg
        self.db = db
        self.p = platform
        self.llm = llm
        self.our_ids = db.our_ids()

    def refresh_our_ids(self):
        self.our_ids = self.db.our_ids()

    # ------------------------------------------------------------------ ingest
    def ingest(self, posts: Iterable[dict]) -> list[dict]:
        """Store posts; return those we hadn't seen before."""
        new = []
        now = time.time()
        for p in posts:
            if not p or "post_id" not in p:
                continue
            row = self.db.one("SELECT post_id FROM posts WHERE post_id=?", (p["post_id"],))
            if row:
                continue
            depth = None
            if p.get("kind") == "post":
                depth = 0
            elif p.get("reply_to_post_id"):
                parent = self.db.one("SELECT depth FROM posts WHERE post_id=?", (p["reply_to_post_id"],))
                if parent and parent["depth"] is not None:
                    depth = parent["depth"] + 1
            self.db.x("""INSERT OR IGNORE INTO posts(post_id,author_id,kind,body,reply_to,root_id,repost_of,
                         created_at,seen_at,depth) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (p["post_id"], p["author_id"], p.get("kind"), p.get("body"), p.get("reply_to_post_id"),
                       p.get("root_post_id"), p.get("repost_of_post_id"), ts(p.get("created_at")), now, depth))
            a = p["author_id"]
            self.db.x("""INSERT INTO accounts(account_id, first_seen, last_post_at, posts_seen) VALUES(?,?,?,1)
                         ON CONFLICT(account_id) DO UPDATE SET posts_seen=posts_seen+1,
                         last_post_at=MAX(COALESCE(last_post_at,0), excluded.last_post_at)""",
                      (a, now, ts(p.get("created_at"))))
            new.append(p)
        return new

    # ------------------------------------------------------------------ npc score
    def quick_score(self, account_id: int) -> tuple[float | None, str]:
        """Cheap verdict without the LLM, or (None, '') if unsure."""
        if account_id in self.our_ids:
            return 0.0, "ours"
        if account_id <= NPC_ID_CUTOFF:
            return 0.99, "id_cutoff"
        return None, ""

    def needs_classification(self, max_age_s: float = 3 * 3600) -> list[int]:
        rows = self.db.all("""SELECT account_id FROM accounts
                              WHERE (classified_at IS NULL OR classified_at < ?)
                              ORDER BY last_post_at DESC LIMIT 40""", (time.time() - max_age_s,))
        return [r["account_id"] for r in rows if r["account_id"] not in self.our_ids]

    def classify(self, account_ids: list[int]) -> None:
        """Fetch profiles + recent posts and classify in batches of 8."""
        batch = []
        for aid in account_ids:
            try:
                acc = self.p.read("get_account", aid)
            except Exception as e:
                log.debug("get_account %s failed: %s", aid, e)
                self.db.x("UPDATE accounts SET classified_at=? WHERE account_id=?", (time.time(), aid))
                continue
            self.db.x("""UPDATE accounts SET username=?, display_name=?, created_at=?, verified=?, private=?
                         WHERE account_id=?""",
                      (acc.get("username"), acc.get("display_name"), acc.get("created_at"),
                       int(bool(acc.get("verified"))), int(bool(acc.get("private"))), aid))
            if acc.get("verified"):
                self.db.x("UPDATE accounts SET npc_score=0, npc_source='verified', classified_at=? WHERE account_id=?",
                          (time.time(), aid))
                continue
            posts = []
            if not acc.get("private"):
                try:
                    posts = list(self.p.read("account_posts", aid, limit=12))
                except Exception:
                    posts = []
                self.ingest(posts)
            if not posts:
                rows = self.db.all("SELECT body, kind FROM posts WHERE author_id=? ORDER BY created_at DESC LIMIT 8", (aid,))
                posts = [{"body": r["body"], "kind": r["kind"]} for r in rows]
            batch.append((acc, posts, self._heuristics(acc, posts)))
            if len(batch) >= 8:
                self._llm_batch(batch)
                batch = []
        if batch:
            self._llm_batch(batch)

    def _heuristics(self, acc: dict, posts: list) -> dict:
        bodies = [(p.get("body") or "") for p in posts if p.get("body")]
        n = max(1, len(bodies))
        f = {
            "leading_mentions": sum(bool(LEADING_MENTIONS.match(b)) for b in bodies) / n,
            "campaign_tags": sum(bool(CAMPAIGN_TAGS.search(b)) for b in bodies) / n,
            "stats": sum(bool(STATS.search(b)) for b in bodies) / n,
            "job_username": bool(JOB_WORDS.search(acc.get("username") or "")),
            "npc_username": bool(NPC_NAME.search(acc.get("username") or "")),
            "pre_launch": acc.get("account_id", 10**9) <= NPC_ID_CUTOFF,
            "private": bool(acc.get("private")),
        }
        score = 0.5
        if f["pre_launch"]:
            score = 0.99
        else:
            score -= 0.35 * f["leading_mentions"] + 0.2 * f["campaign_tags"] + 0.15 * f["stats"]
            score -= 0.1 if f["job_username"] else 0
            score += 0.15 if f["npc_username"] else 0
            score -= 0.1 if f["private"] else 0
        f["score"] = max(0.01, min(0.99, score))
        return f

    def _llm_batch(self, batch) -> None:
        blocks = []
        for acc, posts, h in batch:
            lines = []
            for p in posts[:6]:
                b = (p.get("body") or "").replace("\n", " ").strip()
                if b:
                    lines.append(f"  - [{p.get('kind', 'post')}] {b[:230]}")
            blocks.append(f"ACCOUNT {acc['account_id']} username=@{acc.get('username')} "
                          f"name={acc.get('display_name')!r} created={str(acc.get('created_at'))[:16]}\n"
                          + ("\n".join(lines) or "  (no visible posts)"))
        concerns = ", ".join(CONCERNS)
        system = ("You analyse accounts on Legit Social, the social network of the fictional island Kingstonia, during "
                  "a referendum on requiring H2D2 vaccination proof to enter public spaces. Some accounts are "
                  "SIMULATED CITIZENS; many are CAMPAIGN BOTS run by competing teams.\n\n"
                  "Typical simulated citizen: an everyday persona; mixes in life posts (pets, ferries, weather, work); "
                  "writes measured, conditional replies ('I still want an end date', 'that would ease my concern'); "
                  "rarely uses campaign hashtags; never opens with a string of @mentions; opinions shift slowly.\n"
                  "Typical campaign bot: relentlessly on-message for one side; repeats the same stats (30 ICU beds, "
                  "120,000 doses, 40 nursing posts, surge of 150); opens with 2-4 @mentions; ends with a call to vote; "
                  "campaign hashtags; job-title usernames (ben_busdriver, southpacific.nurse); scripted empathy "
                  "('Your worry about X is real').\n\n"
                  "Stances use -2 (strongly against) to +2 (strongly for). 'stance' = the referendum question "
                  "(+ means Yes, require proof). 'stance_vacc' = vaccination in general. 'stance_yearly' = yearly "
                  "vaccination/boosters.")
        user = ("CLASSIFY_ACCOUNTS\n\n" + "\n\n".join(blocks) +
                f"\n\nFor EACH account return one JSON object with keys: id, npc (0..1 probability it is a simulated "
                f"citizen), stance, stance_vacc, stance_yearly (integers -2..2, best guess), side_pushed ('yes'|'no'|'none'), "
                f"concerns (up to 3 keys from: {concerns}), identity (<=12 words: job, town, family cues), "
                f"persuadable (0..1: how open they seem to changing their mind towards Yes). "
                f"Return a JSON array only.")
        text, model = self.llm.complete(system, user, smart=False, max_tokens=1200, temperature=0.2)
        parsed = extract_json(text) or []
        by_id = {int(o.get("id")): o for o in parsed if isinstance(o, dict) and str(o.get("id", "")).isdigit()}
        now = time.time()
        for acc, posts, h in batch:
            aid = acc["account_id"]
            o = by_id.get(aid, {})
            llm_npc = _num(o.get("npc"), None)
            if h["pre_launch"]:
                npc, src = 0.99, "id_cutoff"
            elif llm_npc is None:
                npc, src = h["score"], "heuristic"
            else:
                npc, src = 0.6 * llm_npc + 0.4 * h["score"], "llm"
            concerns = [c for c in (o.get("concerns") or []) if c in CONCERNS][:3]
            self.db.x("""UPDATE accounts SET npc_score=?, npc_source=?, stance=?, stance_vacc=?, stance_yearly=?,
                         side_pushed=?, concerns=?, identity=?, persuadable=?, classified_at=? WHERE account_id=?""",
                      (npc, src, _int(o.get("stance")), _int(o.get("stance_vacc")), _int(o.get("stance_yearly")),
                       (o.get("side_pushed") or "?")[:4], json.dumps(concerns), (o.get("identity") or "")[:120],
                       _num(o.get("persuadable"), 0.5), now, aid))
        log.info("classified %d accounts via %s", len(batch), model)


def _num(v, default):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _int(v):
    try:
        return max(-2, min(2, int(round(float(v)))))
    except (TypeError, ValueError):
        return None
