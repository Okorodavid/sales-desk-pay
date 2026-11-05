"""SQLite: customers (with what the robot remembers), messages, orders, hand-overs and bank alerts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")
OPEN_STAGES = ("asking", "quoted", "awaiting_payment", "paid")
STAGES = ("asking", "quoted", "awaiting_payment", "paid", "delivered", "cancelled")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,            -- whatsapp | instagram | tiktok | email | telegram | web
    address TEXT NOT NULL,            -- phone, account id, chat id or email
    name TEXT DEFAULT '',
    memory TEXT DEFAULT '{}',         -- what the robot remembers: area, likes, event, notes
    owner_mode INTEGER DEFAULT 0,     -- 1 = the owner took over; the robot stays quiet
    opted_out INTEGER DEFAULT 0,
    created TEXT NOT NULL,
    last_in TEXT DEFAULT '',
    ad_id TEXT DEFAULT '',            -- the Meta ad that started the chat (first touch), from the webhook's referral
    ad_name TEXT DEFAULT '',
    ad_click TEXT DEFAULT '',         -- ctwa_clid (WhatsApp) or ref (Instagram), for sending sales back to Meta later
    ad_at TEXT DEFAULT '',
    UNIQUE(channel, address)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    who TEXT NOT NULL,                -- customer | robot | owner
    text TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'asking',
    items TEXT DEFAULT '[]',          -- [{sku, name, unit, qty, price}]
    zone TEXT DEFAULT '',
    subtotal INTEGER DEFAULT 0,
    delivery_fee INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    says_paid INTEGER DEFAULT 0,      -- the customer says they paid; waiting for proof
    receipt TEXT DEFAULT '',
    paid_by TEXT DEFAULT '',          -- owner | bank_alert
    follow_ups INTEGER DEFAULT 0,
    skip_day TEXT DEFAULT '',         -- local date the owner skipped today's follow-up
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    paid_at TEXT DEFAULT '',
    delivered_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    question TEXT DEFAULT '',
    status TEXT DEFAULT 'open',       -- open | done
    ref TEXT DEFAULT '',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    msg_id TEXT UNIQUE,
    amount INTEGER NOT NULL,
    sender TEXT DEFAULT '',
    narration TEXT DEFAULT '',
    bank TEXT DEFAULT '',
    verified INTEGER DEFAULT 0,
    order_id INTEGER,
    status TEXT NOT NULL,             -- confirmed | to_check | unmatched | owner_confirmed | dismissed
    note TEXT DEFAULT '',
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, calls INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ad_notes (sig TEXT PRIMARY KEY, data TEXT NOT NULL, ts TEXT NOT NULL);
"""
ADDED_COLUMNS = {"customers": ("ad_id", "ad_name", "ad_click", "ad_at")}  # for databases made before these existed


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, cols in ADDED_COLUMNS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT ''")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def reset(self) -> None:
        for table in ("customers", "messages", "orders", "handoffs", "alerts", "usage", "ad_notes"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    # Customers -------------------------------------------------------------------------------
    def customer(self, channel: str, address: str, now: datetime) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM customers WHERE channel=? AND address=?", (channel, address)).fetchone()
        if row:
            return row
        self._exec("INSERT INTO customers(channel, address, created) VALUES(?,?,?)", (channel, address, iso(now)))
        return self.db.execute("SELECT * FROM customers WHERE channel=? AND address=?", (channel, address)).fetchone()

    def get_customer(self, cid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()

    def update_customer(self, cid: int, **fields) -> None:
        if "memory" in fields and isinstance(fields["memory"], dict):
            fields["memory"] = json.dumps(fields["memory"])
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE customers SET {sets} WHERE id=?", (*fields.values(), cid))

    @staticmethod
    def memory(row: sqlite3.Row) -> dict:
        try:
            return json.loads(row["memory"] or "{}")
        except json.JSONDecodeError:
            return {}

    def customers(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM customers ORDER BY id").fetchall()

    def set_ad(self, cid: int, ad_id: str, name: str, click: str, now: datetime) -> None:
        """Remember the ad that started this chat. The first ad wins; later clicks don't move the credit."""
        self._exec("UPDATE customers SET ad_id=?, ad_name=?, ad_click=?, ad_at=? WHERE id=? AND ad_id=''",
                   (ad_id[:64], name[:120], click[:200], iso(now), cid))

    def ad_customers(self, since: datetime) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM customers WHERE ad_id!='' AND ad_at>=? ORDER BY id", (iso(since),)).fetchall()

    def customer_texts(self, cid: int, limit: int = 6) -> list[str]:
        rows = self.db.execute("SELECT text FROM messages WHERE customer_id=? AND who='customer' ORDER BY id DESC LIMIT ?",
                               (cid, limit)).fetchall()
        return [r["text"] for r in reversed(rows)]

    # Ad notes (Muse's advice, cached by the numbers it was given) -----------------------------
    def ad_note(self, sig: str) -> dict | None:
        row = self.db.execute("SELECT data FROM ad_notes WHERE sig=?", (sig,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_ad_note(self, sig: str, data: dict, now: datetime) -> None:
        self._exec("INSERT OR REPLACE INTO ad_notes(sig, data, ts) VALUES(?,?,?)", (sig, json.dumps(data), iso(now)))

    # Messages --------------------------------------------------------------------------------
    def add_message(self, cid: int, who: str, text: str, now: datetime) -> None:
        self._exec("INSERT INTO messages(customer_id, who, text, ts) VALUES(?,?,?,?)", (cid, who, text, iso(now)))
        if who == "customer":
            self._exec("UPDATE customers SET last_in=? WHERE id=?", (iso(now), cid))

    def history(self, cid: int, limit: int = 50) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM messages WHERE customer_id=? ORDER BY id DESC LIMIT ?", (cid, limit)).fetchall()
        return list(reversed(rows))

    def last_message(self, cid: int, who: str | None = None) -> sqlite3.Row | None:
        if who:
            return self.db.execute("SELECT * FROM messages WHERE customer_id=? AND who=? ORDER BY id DESC LIMIT 1",
                                   (cid, who)).fetchone()
        return self.db.execute("SELECT * FROM messages WHERE customer_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()

    def inbound_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.db.execute("SELECT DISTINCT c.* FROM customers c JOIN messages m ON m.customer_id=c.id "
                               "WHERE m.who='customer' AND m.ts>=?", (iso(since),)).fetchall()

    # Orders ----------------------------------------------------------------------------------
    def open_order(self, cid: int) -> sqlite3.Row | None:
        marks = ",".join("?" * len(OPEN_STAGES))
        return self.db.execute(f"SELECT * FROM orders WHERE customer_id=? AND stage IN ({marks}) ORDER BY id DESC LIMIT 1",
                               (cid, *OPEN_STAGES)).fetchone()

    def create_order(self, cid: int, now: datetime) -> sqlite3.Row:
        cur = self._exec("INSERT INTO orders(customer_id, created, updated) VALUES(?,?,?)", (cid, iso(now), iso(now)))
        return self.get_order(cur.lastrowid)

    def get_order(self, oid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()

    def update_order(self, oid: int, now: datetime, **fields) -> None:
        if "items" in fields and not isinstance(fields["items"], str):
            fields["items"] = json.dumps(fields["items"])
        fields["updated"] = iso(now)
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE orders SET {sets} WHERE id=?", (*fields.values(), oid))

    @staticmethod
    def items(row: sqlite3.Row) -> list[dict]:
        try:
            return json.loads(row["items"] or "[]")
        except json.JSONDecodeError:
            return []

    def orders(self, stages: tuple[str, ...] = STAGES) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(stages))
        return self.db.execute(f"SELECT * FROM orders WHERE stage IN ({marks}) ORDER BY updated DESC", stages).fetchall()

    def waiting_for_money(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM orders WHERE stage IN ('quoted','awaiting_payment') AND total>0").fetchall()

    # Hand-overs ------------------------------------------------------------------------------
    def add_handoff(self, cid: int, reason: str, question: str, now: datetime) -> int:
        open_ = self.db.execute("SELECT id FROM handoffs WHERE customer_id=? AND status='open'", (cid,)).fetchone()
        if open_:
            return open_["id"]
        return self._exec("INSERT INTO handoffs(customer_id, reason, question, created) VALUES(?,?,?,?)",
                          (cid, reason, question[:500], iso(now))).lastrowid

    def open_handoffs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM handoffs WHERE status='open' ORDER BY id").fetchall()

    def get_handoff(self, hid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM handoffs WHERE id=?", (hid,)).fetchone()

    def close_handoffs(self, cid: int) -> None:
        self._exec("UPDATE handoffs SET status='done' WHERE customer_id=? AND status='open'", (cid,))

    def set_handoff_ref(self, hid: int, ref: str) -> None:
        self._exec("UPDATE handoffs SET ref=? WHERE id=?", (ref, hid))

    def handoff_by_ref(self, ref: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM handoffs WHERE ref=?", (ref,)).fetchone()

    # Bank alerts -----------------------------------------------------------------------------
    def seen_alert(self, msg_id: str) -> bool:
        return bool(msg_id) and self.db.execute("SELECT 1 FROM alerts WHERE msg_id=?", (msg_id,)).fetchone() is not None

    def add_alert(self, now: datetime, **fields) -> int:
        fields["ts"] = iso(now)
        cols = ", ".join(fields)
        return self._exec(f"INSERT INTO alerts({cols}) VALUES({','.join('?' * len(fields))})", tuple(fields.values())).lastrowid

    def get_alert(self, aid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()

    def update_alert(self, aid: int, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE alerts SET {sets} WHERE id=?", (*fields.values(), aid))

    def alerts(self, statuses: tuple[str, ...]) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self.db.execute(f"SELECT * FROM alerts WHERE status IN ({marks}) ORDER BY id DESC", statuses).fetchall()

    # Cost guard ------------------------------------------------------------------------------
    def bump_calls(self, day: str) -> int:
        self._exec("INSERT INTO usage(day, calls) VALUES(?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (day,))
        return self.db.execute("SELECT calls FROM usage WHERE day=?", (day,)).fetchone()["calls"]
