"""SQLite storage. Everything the bot learns survives restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS our_accounts (
    account_id   INTEGER PRIMARY KEY,
    username     TEXT UNIQUE NOT NULL,
    password     TEXT NOT NULL,
    display_name TEXT,
    persona_key  TEXT NOT NULL,
    token        TEXT,
    status       TEXT NOT NULL DEFAULT 'active',   -- active | retired | suspended | lost
    actions      INTEGER NOT NULL DEFAULT 0,
    posts_made   INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    last_used    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS accounts (               -- everyone else we have seen
    account_id   INTEGER PRIMARY KEY,
    username     TEXT,
    display_name TEXT,
    created_at   TEXT,
    verified     INTEGER DEFAULT 0,
    private      INTEGER DEFAULT 0,
    npc_score    REAL,              -- 0..1 probability this is a simulated citizen
    npc_source   TEXT,              -- id_cutoff | heuristic | llm
    stance       INTEGER,           -- -2 hard No .. +2 hard Yes, on the referendum question
    stance_vacc  INTEGER,           -- views on vaccination in general
    stance_yearly INTEGER,          -- views on yearly vaccination
    side_pushed  TEXT,              -- for competitor bots: which side they push (yes/no/?)
    concerns     TEXT,              -- json list of concern keys
    identity     TEXT,              -- short free text: job, town, family
    persuadable  REAL,              -- 0..1
    posts_seen   INTEGER DEFAULT 0,
    first_seen   REAL,
    last_post_at REAL,              -- epoch of their newest post we saw
    last_targeted REAL DEFAULT 0,
    times_targeted INTEGER DEFAULT 0,
    moved_until  REAL DEFAULT 0,    -- don't push again until (they signalled a shift)
    classified_at REAL DEFAULT 0,
    actions_est  INTEGER DEFAULT 0,
    hidden_posts INTEGER DEFAULT 0,
    reported_at  REAL DEFAULT 0,
    suspended    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    post_id      INTEGER PRIMARY KEY,
    author_id    INTEGER,
    kind         TEXT,
    body         TEXT,
    reply_to     INTEGER,
    root_id      INTEGER,
    repost_of    INTEGER,
    created_at   REAL,
    seen_at      REAL,
    depth        INTEGER,
    status       TEXT DEFAULT 'visible'      -- visible | hidden | gone
);
CREATE INDEX IF NOT EXISTS posts_author ON posts(author_id);
CREATE INDEX IF NOT EXISTS posts_created ON posts(created_at);

CREATE TABLE IF NOT EXISTS our_posts (
    post_id      INTEGER PRIMARY KEY,
    account_id   INTEGER,
    persona_key  TEXT,
    kind         TEXT,               -- reply | post | life | continuation
    argument     TEXT,
    target_id    INTEGER,            -- NPC we aimed at
    target_post  INTEGER,
    mentions     TEXT,               -- json list of account ids
    body         TEXT,
    model        TEXT,
    created_at   REAL,
    npc_likes    INTEGER DEFAULT 0,
    npc_replies  INTEGER DEFAULT 0,
    npc_reposts  INTEGER DEFAULT 0,
    shift_score  REAL DEFAULT 0,     -- LLM-judged movement towards our side in NPC replies
    checked_at   REAL DEFAULT 0,
    status       TEXT DEFAULT 'visible'
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT,               -- post | account
    target_id    INTEGER,
    author_id    INTEGER,
    confidence   REAL,
    detail       TEXT,
    created_at   REAL,
    status       TEXT DEFAULT 'pending',   -- pending | inauthentic | genuine | dismissed | error
    resolved_at  REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS reports_unique ON reports(kind, target_id);

CREATE TABLE IF NOT EXISTS bandit (
    arm          TEXT PRIMARY KEY,   -- e.g. arg:hospital_capacity or persona:nurse_mere
    pulls        REAL DEFAULT 0,
    reward       REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key          TEXT PRIMARY KEY,
    value        TEXT
);

CREATE TABLE IF NOT EXISTS follows (               -- citizens our accounts follow (once each)
    account_id   INTEGER,
    target_id    INTEGER,
    created_at   REAL,
    PRIMARY KEY (account_id, target_id)
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day          TEXT,
    model        TEXT,
    calls        INTEGER DEFAULT 0,
    cost_usd     REAL DEFAULT 0,
    PRIMARY KEY (day, model)
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           REAL,
    kind         TEXT,
    data         TEXT
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(our_posts)")}
        if "npc_reply_sample" not in cols:
            self.conn.execute("ALTER TABLE our_posts ADD COLUMN npc_reply_sample TEXT")

    # ---- generic -----------------------------------------------------------
    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    # Fetch inside the lock too: drafting, classifying and mining all share this connection from threads.
    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))

    def event(self, kind: str, **data: Any) -> None:
        self.x("INSERT INTO events(at,kind,data) VALUES(?,?,?)", (time.time(), kind, json.dumps(data, default=str)))

    # ---- our accounts -----------------------------------------------------
    def add_our_account(self, account_id: int, username: str, password: str, display_name: str,
                        persona_key: str, token: Optional[str]) -> None:
        self.x("""INSERT OR REPLACE INTO our_accounts
                  (account_id,username,password,display_name,persona_key,token,status,created_at)
                  VALUES(?,?,?,?,?,?,'active',?)""",
               (account_id, username, password, display_name, persona_key, token, time.time()))

    def our_accounts(self, status: str = "active") -> list:
        return self.all("SELECT * FROM our_accounts WHERE status=? ORDER BY last_used", (status,))

    def our_ids(self) -> set:
        return {r["account_id"] for r in self.all("SELECT account_id FROM our_accounts")}

    def count_our_accounts(self) -> int:
        return self.one("SELECT COUNT(*) c FROM our_accounts")["c"]

    # ---- LLM spend ----------------------------------------------------------
    def add_llm_cost(self, model: str, cost: float) -> None:
        day = time.strftime("%Y-%m-%d")
        self.x("""INSERT INTO llm_usage(day,model,calls,cost_usd) VALUES(?,?,1,?)
                  ON CONFLICT(day,model) DO UPDATE SET calls=calls+1, cost_usd=cost_usd+excluded.cost_usd""",
               (day, model, cost))

    def llm_cost_today(self) -> float:
        row = self.one("SELECT COALESCE(SUM(cost_usd),0) c FROM llm_usage WHERE day=?", (time.strftime("%Y-%m-%d"),))
        return float(row["c"])
