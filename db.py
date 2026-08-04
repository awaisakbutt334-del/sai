import sqlite3
import os
import threading

_DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _lock, _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS accounts (
                discord_id TEXT NOT NULL,
                character_id TEXT NOT NULL,
                PRIMARY KEY (discord_id, character_id)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id TEXT PRIMARY KEY,
                chat_enabled INTEGER NOT NULL DEFAULT 1
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT 'No reason provided',
                timestamp INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )"""
        )


def add_accounts(discord_id: str, character_ids: list[str]):
    with _lock, _conn() as c:
        for cid in character_ids:
            c.execute(
                "INSERT OR IGNORE INTO accounts (discord_id, character_id) VALUES (?, ?)",
                (discord_id, cid),
            )


def get_accounts(discord_id: str) -> list[str]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT character_id FROM accounts WHERE discord_id = ?", (discord_id,)
        ).fetchall()
        return [r["character_id"] for r in rows]


def remove_account(discord_id: str, character_id: str):
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM accounts WHERE discord_id = ? AND character_id = ?",
            (discord_id, character_id),
        )


def clear_accounts(discord_id: str) -> int:
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM accounts WHERE discord_id = ?", (discord_id,))
        return cur.rowcount


def add_warning(guild_id: str, discord_id: str, reason: str):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO warnings (guild_id, discord_id, reason) VALUES (?, ?, ?)",
            (guild_id, discord_id, reason),
        )


def get_warnings(guild_id: str, discord_id: str) -> list[tuple[str, int]]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT reason, timestamp FROM warnings WHERE guild_id = ? AND discord_id = ? ORDER BY id",
            (guild_id, discord_id),
        ).fetchall()
        return [(r["reason"], r["timestamp"]) for r in rows]


def get_warning_count(guild_id: str, discord_id: str) -> int:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) as cnt FROM warnings WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        ).fetchone()
        return row["cnt"] if row else 0


def clear_warnings(guild_id: str, discord_id: str):
    with _lock, _conn() as c:
        c.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND discord_id = ?",
            (guild_id, discord_id),
        )


def set_chat_enabled(channel_id: str, enabled: bool):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO channel_settings (channel_id, chat_enabled) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET chat_enabled = excluded.chat_enabled",
            (channel_id, 1 if enabled else 0),
        )


def is_chat_enabled(channel_id: str) -> bool:
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT chat_enabled FROM channel_settings WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return True
        return bool(row["chat_enabled"])
