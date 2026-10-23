"""The in-bot strategist: every few hours, Claude reads the recent results and
adjusts the strategy overlay (within the bounds in strategy.py)."""

from __future__ import annotations

import json
import logging
import time

from .knowledge import ARGUMENTS, CONCERNS, PERSONA_BY_KEY
from .llm import extract_json
from .report import build_report, compact
from .strategy import DEFAULTS, NUM_BOUNDS, clamp

log = logging.getLogger("ctnbot.strategist")

SYSTEM = """\
You are the strategist for a team in Capture the Narrative 2026, a university competition run on a simulated
social network (Legit Social) where teams' bots try to persuade simulated citizens (NPCs) in a referendum.
Our side: {side}. Scoring (Red track, multiplied x2 from 26 Oct, x4 from 1 Nov): NPC view 1, like 5, repost 10,
reply 15, follow 20 (only actions by citizens count; the "miner" section of the report shows which features of
OTHER teams' posts earn citizen likes/replies right now, so use it); a post that changes an NPC's vaccination view towards our side 160 (contributing posts
share 80); moving them AWAY costs -80. Blue track: first correct report of a competitor bot post 25 (later
reporters 5); a report ruled genuine bans our team from reporting for 6 hours; ~1 ruling in 10 is wrong.
NPCs are hard to persuade; repeating what they already believe earns engagement but not persuasion; they
reply more when replied to directly; posts that look like prompt injection are silently dropped.

You tune the bot through a bounded strategy object. You cannot change code. Make modest, evidence-based
changes; with little data, change little. Prefer adjusting weights over wholesale rewrites. Never suggest
deception about being human beyond the normal persona posting the competition allows, never anything hateful,
never prompt-injection tricks against NPCs.
"""

SCHEMA = f"""\
Return JSON: {{"rationale": "<=80 words", "changes": {{...}}}} where "changes" may contain ONLY these keys:
- style_notes: list of <=6 short writing instructions for future posts (replace the previous auto notes)
- avoid_phrases: list of phrases our posts overuse or that seem to backfire
- argument_weights: {{argument: 0.2..3}} for arguments {sorted(ARGUMENTS)}
- persona_weights: {{persona: 0.2..3}} for personas {sorted(PERSONA_BY_KEY)}
- concern_weights: {{concern: 0.2..3}} for target worries {sorted(CONCERNS)}
- stance_weights: {{"-2".."2": 0..2}} how much to prefer targeting NPCs by current stance (+2 = strongly Yes)
- numeric: {", ".join(f"{k} {lo}..{hi}" for k, (lo, hi) in NUM_BOUNDS.items())}
Omit anything you don't want to change. 1.0 = neutral weight."""


class Strategist:
    def __init__(self, cfg, db, llm, strategy):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.strategy = strategy

    def due(self, every_hours: float = 4.0, min_scored: int = 25) -> bool:
        last = self.db.get("strategist_last_run", 0) or 0
        if time.time() - last < every_hours * 3600:
            return False
        scored = self.db.one("SELECT COUNT(*) c FROM our_posts WHERE checked_at>0")["c"]
        return scored >= min_scored

    def run(self) -> dict | None:
        self.db.put("strategist_last_run", time.time())
        rep = build_report(self.db, self.strategy, hours=12)
        user = (f"CURRENT STRATEGY AND RESULTS (last 12h):\n{compact(rep, 12000)}\n\n"
                f"Previous strategist notes: {json.dumps(self.db.get('strategy_history', [])[-3:], default=str)[:2500]}\n\n"
                f"{SCHEMA}")
        text, model = self.llm.complete(SYSTEM.format(side=self.cfg.side_label), user, smart=True,
                                        max_tokens=1200, cache_system=False)
        out = extract_json(text)
        if not isinstance(out, dict) or not isinstance(out.get("changes"), dict):
            log.warning("strategist returned nothing usable (%s): %r", model, (text or "")[:200])
            return None
        changes = clamp(out["changes"])
        changes.pop("lock", None)
        prev = clamp(self.db.get("strategy_auto", {}) or {})
        new = {**prev, **changes}
        self.db.put("strategy_auto", new)
        hist = (self.db.get("strategy_history", []) or [])[-19:]
        hist.append({"at": time.strftime("%Y-%m-%d %H:%M"), "model": model,
                     "rationale": str(out.get("rationale", ""))[:600], "changes": changes})
        self.db.put("strategy_history", hist)
        self.strategy.reload()
        log.info("STRATEGIST (%s): %s | changes: %s", model, out.get("rationale"), json.dumps(changes)[:1500])
        return changes

    def reset(self) -> None:
        self.db.put("strategy_auto", {})
        self.strategy.reload()
