"""Owner-only SQLite state. Secrets never leave the local API in responses."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.path = root / "autotunnel.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    zone_id TEXT PRIMARY KEY,
                    zone_name TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL DEFAULT '',
                    cert_pem TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tunnels (
                    id TEXT PRIMARY KEY,
                    tunnel_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    zone_id TEXT NOT NULL,
                    zone_name TEXT NOT NULL,
                    hostname TEXT NOT NULL UNIQUE,
                    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
                    credentials_json TEXT NOT NULL,
                    host_mode TEXT NOT NULL DEFAULT 'auto',
                    origin_host TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tunnels)")}
            if "host_mode" not in columns:
                db.execute("ALTER TABLE tunnels ADD COLUMN host_mode TEXT NOT NULL DEFAULT 'auto'")
            if "origin_host" not in columns:
                db.execute("ALTER TABLE tunnels ADD COLUMN origin_host TEXT NOT NULL DEFAULT ''")
        os.chmod(self.path, 0o600)

    def upsert_account(self, zone_id: str, account_id: str, cert_pem: str, zone_name: str = "") -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO accounts(zone_id,zone_name,account_id,cert_pem,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(zone_id) DO UPDATE SET
                zone_name=CASE WHEN excluded.zone_name='' THEN accounts.zone_name ELSE excluded.zone_name END,
                account_id=excluded.account_id, cert_pem=excluded.cert_pem, updated_at=excluded.updated_at""",
                (zone_id, zone_name, account_id, cert_pem, int(time.time())))

    def account(self, zone_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE zone_id=?", (zone_id,)).fetchone()
            return dict(row) if row else None

    def accounts(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT zone_id,zone_name,account_id,updated_at FROM accounts ORDER BY zone_name", ()).fetchall()
            return [dict(row) for row in rows]

    def set_zone_name(self, zone_id: str, zone_name: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE accounts SET zone_name=? WHERE zone_id=?", (zone_name, zone_id))

    def tunnels(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tunnels ORDER BY created_at DESC").fetchall()
            return [dict(row) for row in rows]

    def tunnel(self, id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tunnels WHERE id=?", (id,)).fetchone()
            return dict(row) if row else None

    def by_hostname(self, hostname: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM tunnels WHERE hostname=?", (hostname,)).fetchone()
            return dict(row) if row else None

    def add_tunnel(self, item: dict[str, Any]) -> None:
        now = int(time.time())
        with self._connect() as db:
            db.execute("""INSERT INTO tunnels(id,tunnel_id,name,zone_id,zone_name,hostname,port,credentials_json,host_mode,origin_host,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (item["id"], item["tunnel_id"], item["name"], item["zone_id"],
                item["zone_name"], item["hostname"], item["port"], item["credentials_json"],
                item.get("host_mode", "auto"), item.get("origin_host", ""), int(item.get("enabled", True)), now, now))

    def update_tunnel(self, id: str, **fields: Any) -> None:
        allowed = {"name", "hostname", "port", "enabled", "zone_id", "zone_name", "host_mode", "origin_host"}
        if not fields or set(fields) - allowed:
            raise ValueError("unsupported tunnel fields")
        fields["updated_at"] = int(time.time())
        clause = ", ".join(f"{key}=?" for key in fields)
        with self._connect() as db:
            db.execute(f"UPDATE tunnels SET {clause} WHERE id=?", (*fields.values(), id))

    def delete_tunnel(self, id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM tunnels WHERE id=?", (id,))
