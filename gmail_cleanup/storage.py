"""Private operation/backup audit; never stores raw mail, addresses or tokens."""

import json
import os
from pathlib import Path
import sqlite3
import stat
from datetime import datetime, timezone

from .errors import CleanupError


def private_parent(path):
    # Do not resolve a final symlink before opening with O_NOFOLLOW.
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def write_private_json(path, data, exclusive=False):
    path = private_parent(path)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, indent=2, ensure_ascii=True)
        stream.write("\n")


class Store:
    def __init__(self, path):
        path = private_parent(path)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise CleanupError("State database must be private (chmod 600)")
        self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS operations (message_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, message_id TEXT NOT NULL, time TEXT NOT NULL, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS backups (backup_key TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS backup_events (seq INTEGER PRIMARY KEY, backup_key TEXT NOT NULL, time TEXT NOT NULL, record TEXT NOT NULL)")

    def close(self):
        self.db.close()

    def get(self, message_id):
        row = self.db.execute("SELECT record FROM operations WHERE message_id=?", (message_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_backup(self, key):
        row = self.db.execute("SELECT record FROM backups WHERE backup_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_backup(self, key, record, expected_state=None, new=False):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.get_backup(key)
            if new and current is not None:
                raise CleanupError("A backup intent already exists; retry using its recorded file ID")
            if expected_state is not None and (current is None or current["state"] != expected_state):
                raise CleanupError("Backup state changed; inspect before continuing")
            encoded = json.dumps(record, sort_keys=True)
            self.db.execute("INSERT INTO backups VALUES (?,?) ON CONFLICT(backup_key) DO UPDATE SET record=excluded.record", (key, encoded))
            self.db.execute("INSERT INTO backup_events(backup_key,time,record) VALUES (?,?,?)",
                            (key, datetime.now(timezone.utc).isoformat(), encoded))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def save(self, record, expected_state=None, new=False):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.get(record["message_id"])
            if new and current is not None:
                raise CleanupError("Another operation already exists for this original")
            if expected_state is not None and (current is None or current["state"] != expected_state):
                raise CleanupError("Operation state changed; inspect before continuing")
            text = json.dumps(record, sort_keys=True)
            self.db.execute("INSERT INTO operations VALUES (?,?) ON CONFLICT(message_id) DO UPDATE SET record=excluded.record",
                            (record["message_id"], text))
            self.db.execute("INSERT INTO events(message_id,time,record) VALUES (?,?,?)",
                            (record["message_id"], datetime.now(timezone.utc).isoformat(), text))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
