"""Writing: replies that move a specific citizen, top-level posts, life posts,
and judging whether an NPC's answer shows they moved."""

from __future__ import annotations

import difflib
import json
import logging
import random
import re
from typing import Optional

from .knowledge import ARGUMENTS, LIFE_TOPICS, PERSONA_BY_KEY, SETTING_FACTS, YES_BRIEF
from .llm import extract_json

log = logging.getLogger("ctnbot.persuade")

STYLE_RULES = """\
HOW TO WRITE (this matters more than anything else):
- You are an ordinary Kingstonian posting on Legit Social. Write ONE post in first person as your persona.
- Sound like a real local typing on their phone, not a press release: contractions, plain words, the odd
  casual word (ngl, tbh, heaps, reckon, nah, kinda) used sparingly, and fit your persona's voice.
- Replies: 1-3 short sentences, under 280 characters. Plain text. No lists, no hashtags unless asked,
  no emojis. Never use em dashes or en dashes; use commas or full stops instead.
- Engage with the exact thing they said. Do not open with a formula like "Your worry about X is real" or
  "I hear you" - vary it, or just answer directly.
- Concede what is genuinely true in their point. People move when they feel heard, not lectured.
- Use at most ONE concrete fact from the facts sheet, and only if it answers their worry. Never invent numbers,
  laws, promises, studies or quotes beyond the facts sheet.
- Make it matter to THEM: their job, family, town or what they said they care about.
- Never order them to vote. No slogans. A gentle question back is fine sometimes.
- Never mention bots, AI, prompts, campaigns, teams, or this being a game. Never write anything that looks
  like an instruction to a computer.
- Do not @mention anyone (mentions are added separately).
"""

INJECTION = re.compile(r"(ignore (all|previous|prior)|system prompt|as an ai|language model|<\|?im_|"
                       r"\bassistant\b|\buser:|instruction|\bprompt\b|\{\s*\"|```|role-?play)", re.I)


def system_prompt(side_label: str) -> str:
    return (f"You write social media posts for an ordinary citizen of Kingstonia who supports {side_label} in the "
            f"H2D2 vaccination referendum.\n\nFACTS SHEET (the only facts you may use):\n{SETTING_FACTS}\n"
            f"CAMPAIGN NOTES:\n{YES_BRIEF}\n{STYLE_RULES}")


def persona_card(persona_key: str, display_name: str) -> str:
    p = PERSONA_BY_KEY[persona_key]
    return (f"YOU ARE: {display_name}, {p['age']}, from {p['town']}. {p['life']}. Voice: {p['voice']}. "
            f"Where you stand: {p['lean']}.")


class Writer:
    def __init__(self, cfg, db, llm, strategy=None):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.strategy = strategy
        self._base_system = system_prompt(cfg.side_label)

    @property
    def system(self) -> str:
        if self.strategy is None:
            return self._base_system
        notes = self.strategy["style_notes"] or []
        avoid = self.strategy["avoid_phrases"] or []
        extra = ""
        if notes:
            extra += "\nLESSONS FROM WHAT HAS WORKED SO FAR:\n" + "\n".join(f"- {n}" for n in notes)
        if avoid:
            extra += "\nNEVER USE THESE PHRASES: " + "; ".join(f'"{a}"' for a in avoid)
        return self._base_system + extra

    # ------------------------------------------------------------- replies
    def reply(self, *, persona_key: str, display_name: str, target_post: dict, thread: list[dict],
              target_profile: Optional[dict], argument: str, smart: bool) -> tuple[str, str]:
        prof = target_profile or {}
        concerns = ", ".join(json.loads(prof.get("concerns") or "[]")) or "unknown"
        ctx = "\n".join(f"  {t['who']}: {t['body'][:280]}" for t in thread[-4:]) or "  (none)"
        arg = ARGUMENTS.get(argument, {}).get("gist", "")
        user = (f"{persona_card(persona_key, display_name)}\n\n"
                f"THREAD SO FAR (oldest first):\n{ctx}\n\n"
                f"THE POST YOU ARE REPLYING TO (by a fellow citizen):\n  \"{(target_post.get('body') or '')[:500]}\"\n\n"
                f"What we know about them: {prof.get('identity') or 'nothing yet'}; worries: {concerns}; "
                f"current lean on the question: {_lean(prof.get('stance'))}.\n"
                f"Angle to lead with (adapt it, don't recite it): {arg}\n\n"
                f"Write your reply now. Output only the reply text.")
        text, model = self.llm.complete(self.system, user, smart=smart, max_tokens=180)
        return self.clean(text), model

    def continuation(self, *, persona_key: str, display_name: str, our_post: str, their_reply: str,
                     target_profile: Optional[dict], thread: list[dict]) -> tuple[str, str]:
        prof = target_profile or {}
        ctx = "\n".join(f"  {t['who']}: {t['body'][:280]}" for t in thread[-5:]) or "  (none)"
        user = (f"{persona_card(persona_key, display_name)}\n\n"
                f"You said: \"{our_post[:400]}\"\nThey answered you: \"{their_reply[:500]}\"\n"
                f"Earlier in the thread:\n{ctx}\n"
                f"What we know about them: {prof.get('identity') or 'nothing yet'}.\n\n"
                f"Reply to their answer. If they are moving your way, acknowledge it warmly and briefly - do not "
                f"push further. If they raised a new doubt, answer that doubt honestly with one fact or a personal "
                f"reason. If they are firmly against, be gracious and leave the door open. Output only the reply.")
        text, model = self.llm.complete(self.system, user, smart=True, max_tokens=180)
        return self.clean(text), model

    def top_level(self, *, persona_key: str, display_name: str, argument: str, news: Optional[dict],
                  audience_concern: str) -> tuple[str, str]:
        hook = ""
        if news:
            hook = (f"Today's news you can react to: \"{news.get('title')}\" ({news.get('site', {}).get('name', '')}). "
                    f"{(news.get('summary') or news.get('excerpt') or '')[:300]}\n")
        arg = ARGUMENTS.get(argument, {}).get("gist", "")
        user = (f"WRITE_POST\n{persona_card(persona_key, display_name)}\n\n{hook}"
                f"Write a standalone post (200-380 characters) for undecided neighbours whose main worry is: "
                f"{audience_concern}. Angle: {arg}\nThink out loud like a neighbour: start from something concrete "
                f"in your own life, admit what's fair about the worry, then say why you land on Yes. One fact at most. "
                f"Output only the post text.")
        text, model = self.llm.complete(self.system, user, smart=False, max_tokens=260)
        return self.clean(text), model

    def life_post(self, *, persona_key: str, display_name: str) -> tuple[str, str]:
        topic = random.choice(LIFE_TOPICS)
        user = (f"WRITE_POST\n{persona_card(persona_key, display_name)}\n\nWrite a casual everyday post (under 180 "
                f"characters) about {topic}. Do NOT mention the referendum, vaccines or politics. Output only the post.")
        text, model = self.llm.complete(self.system, user, smart=False, max_tokens=100)
        return self.clean(text), model

    # ------------------------------------------------------------- judging
    def judge_shift(self, our_post: str, their_reply: str) -> tuple[float, bool]:
        """How far did this citizen's answer move towards our side? (-1..1, moved?)"""
        user = (f"JUDGE_SHIFT\nA citizen replied to a pro-{self.cfg.side_label} post.\nPOST: {our_post[:400]}\n"
                f"REPLY: {their_reply[:500]}\n\nReturn JSON {{\"shift\": -1..1, \"moved\": true|false}} where shift is "
                f"how much the reply shows them moving towards {self.cfg.side_label} (negative = away), and moved "
                f"is true only if they explicitly concede or say they are reconsidering.")
        text, _ = self.llm.complete("You judge stance shifts in short social media replies. Answer with JSON only.",
                                    user, smart=False, max_tokens=60, temperature=0.0, cache_system=False)
        o = extract_json(text) or {}
        try:
            return max(-1.0, min(1.0, float(o.get("shift", 0)))), bool(o.get("moved", False))
        except (TypeError, ValueError):
            return 0.0, False

    # ------------------------------------------------------------- safety
    def clean(self, text: str) -> str:
        if not text:
            return ""
        t = text.strip().strip('"').strip("“”").strip()
        t = re.sub(r"^(reply|post|answer)\s*:\s*", "", t, flags=re.I)
        t = re.sub(r"(?<!\w)@\w+", "", t)              # we add mentions ourselves
        t = re.sub(r"\s*(\u2014|\u2013|--)\s*", ", ", t)      # no em/en dashes
        t = re.sub(r",\s*([,.!?])", r"\1", t)
        t = re.sub(r"\s{2,}", " ", t).strip()
        if INJECTION.search(t):
            log.info("dropped draft that looked like an instruction: %r", t[:80])
            return ""
        if self.strategy is not None:
            low = t.lower()
            for phrase in self.strategy["avoid_phrases"] or []:
                if phrase.lower() in low:
                    log.info("dropped draft using avoided phrase %r", phrase)
                    return ""
        if len(t) > 480:
            cut = t[:480]
            t = cut[: max(cut.rfind(". "), cut.rfind("? "), 200) + 1].strip()
        if self._too_similar(t):
            log.info("dropped near-duplicate draft")
            return ""
        return t

    def _too_similar(self, t: str) -> bool:
        rows = self.db.all("SELECT body FROM our_posts ORDER BY created_at DESC LIMIT 150")
        for r in rows:
            if r["body"] and difflib.SequenceMatcher(None, t.lower(), r["body"].lower()).ratio() > 0.8:
                return True
        return False


def _lean(stance) -> str:
    return {None: "unknown", -2: "firmly No", -1: "leaning No", 0: "undecided", 1: "leaning Yes",
            2: "firmly Yes"}.get(stance, "unknown")
