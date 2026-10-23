#!/usr/bin/env python3
"""ctnbot command line.

  python run.py check              verify config, Legit Social, and the Claude key
  python run.py observe            read + classify only (no posts, no reports)
  python run.py run                the full bot (posts, replies, reports) — what Railway runs
  python run.py run --minutes 30   run for a fixed time
  python run.py status             counters from the local database
  python run.py report             the full JSON report the strategist and daily review read
  python run.py strategist         run the strategist once now
  python run.py top                which arguments / personas earn the most NPC engagement
  python run.py accounts           list our accounts (username, persona, status, actions)
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import logging.handlers
import signal
import sys
import time

from ctnbot.config import Config

RECENT_ERRORS: collections.deque = collections.deque(maxlen=30)


class _ErrorBuffer(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            RECENT_ERRORS.append(f"{time.strftime('%m-%d %H:%M')} {record.levelname} {record.getMessage()[:240]}")
        except Exception:
            pass


def setup_logging(cfg: Config) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    root.addHandler(out)
    try:
        f = logging.handlers.RotatingFileHandler(cfg.path("ctnbot.log"), maxBytes=20_000_000, backupCount=3)
        f.setFormatter(fmt)
        root.addHandler(f)
    except OSError:
        pass
    eb = _ErrorBuffer(level=logging.WARNING)
    root.addHandler(eb)
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "observe", "run", "status", "report", "strategist", "top",
                                        "accounts"])
    ap.add_argument("--minutes", type=float, default=None)
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    cfg = Config.load(args.env)
    setup_logging(cfg)
    if args.command == "observe":
        cfg.observe_only = True

    if args.command in ("status", "top", "accounts", "report"):
        from ctnbot.bot import status
        from ctnbot.db import DB
        from ctnbot.report import build_report
        from ctnbot.strategy import Strategy
        db = DB(cfg.db_path)
        if args.command == "status":
            print(json.dumps(status(db), indent=2))
        elif args.command == "report":
            print(json.dumps(build_report(db, Strategy(db)), indent=2, default=str))
        elif args.command == "top":
            for r in db.all("SELECT arm, pulls, reward, reward/MAX(pulls,1) avg FROM bandit ORDER BY avg DESC"):
                print(f"{r['arm']:<34} pulls={r['pulls']:>5.0f} reward={r['reward']:>7.1f} avg={r['avg']:.2f}")
        else:
            for r in db.all("SELECT account_id, username, persona_key, status, actions FROM our_accounts"):
                print(dict(r))
        return

    if args.command == "check":
        return check(cfg)

    from ctnbot.bot import Bot
    bot = Bot(cfg)
    if args.command == "strategist":
        print(json.dumps(bot.strategist.run(), indent=2))
        return

    def _stop(signum, frame):            # Railway sends SIGTERM on redeploy
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    _maybe_health_server()

    def _flush_errors():
        bot.db.put("recent_errors", list(RECENT_ERRORS))

    bot.on_tick = _flush_errors
    try:
        bot.run(duration=args.minutes * 60 if args.minutes else None)
    except KeyboardInterrupt:
        logging.getLogger("ctnbot").info("stopping cleanly")
        _flush_errors()


def _maybe_health_server() -> None:
    """If the host gives us a PORT (e.g. a Render/Railway web service), answer health checks on it."""
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    port = os.environ.get("PORT")
    if not port:
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def check(cfg: Config) -> None:
    ok = True
    print(f"side: {cfg.side_label}   data: {cfg.data_dir}   dry_run={cfg.dry_run} fake={cfg.fake_platform}")
    if not cfg.team_code:
        print("✗ CTN_TEAM_CODE is not set")
        ok = False
    else:
        print(f"✓ team code set (…{cfg.team_code[-3:]})")
    try:
        import capturethenarrative as ctn
        s = ctn.LegitSocial()
        posts = s.feed_latest(limit=3)
        print(f"✓ Legit Social reachable ({len(posts)} posts read)")
    except Exception as e:
        print(f"✗ Legit Social: {e}")
        ok = False
    if cfg.anthropic_api_key:
        from ctnbot.db import DB
        from ctnbot.llm import LLM
        llm = LLM(cfg, DB(cfg.db_path))
        for model in (cfg.model_fast, cfg.model_smart):
            try:
                text = llm._claude(model, "Reply with the single word ok.", "Say ok", 20, False, 0)
                print(f"✓ Claude reachable ({model}): {text!r}")
            except Exception as e:
                print(f"✗ Claude {model}: {e}")
                ok = False
    else:
        print("! ANTHROPIC_API_KEY not set — falls back to the competition LLM (much weaker)")
    print("\nAll good." if ok else "\nFix the ✗ items above.")


if __name__ == "__main__":
    main()
