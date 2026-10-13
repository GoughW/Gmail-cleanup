"""Write-once hash-chained recovery history, separate from SQLite restores.

This detects database-only rollback, not rollback/loss of all copies of history.
"""
import json
import os
from pathlib import Path
import stat
import uuid

from .errors import CleanupError
from .mime import json_digest
from .storage import write_private_json


class Journal:
    def __init__(self, path, initialize=False, mailbox_key=None):
        self.path = Path(path).absolute()
        if initialize:
            self.path.mkdir(mode=0o700, parents=False, exist_ok=False)
            parent_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            header = {"version": 1, "store_id": uuid.uuid4().hex, "mailbox_key": mailbox_key}
            write_private_json(self.path / "genesis.json", header, exclusive=True, authoritative=True)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(self.fd)
            raise CleanupError("Recovery directory must be private and owned")
        self.identity = info.st_dev, info.st_ino
        try:
            self.header = self.read("genesis.json")
            if (set(self.header) != {"version", "store_id", "mailbox_key"}
                    or self.header["version"] != 1 or not self.header["mailbox_key"]):
                raise CleanupError("Malformed recovery genesis; recovery review required")
            self.records()
        except BaseException:
            os.close(self.fd)
            raise

    def read(self, name):
        if (os.stat(self.path).st_dev, os.stat(self.path).st_ino) != self.identity:
            raise CleanupError("Recovery directory changed")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise CleanupError("Recovery record is aliased or not private")
            with os.fdopen(fd, "r") as stream:
                fd = None
                return json.load(stream)
        except (ValueError, TypeError):
            raise CleanupError("Torn/malformed recovery record; mutations prohibited") from None
        finally:
            if fd is not None:
                os.close(fd)

    def records(self):
        names = sorted(os.listdir(self.fd))
        if "genesis.json" not in names:
            raise CleanupError("Missing recovery genesis")
        names.remove("genesis.json")
        previous = json_digest(self.header)
        result = []
        for sequence, name in enumerate(names, 1):
            if name != f"{sequence:012d}.json":
                raise CleanupError("Missing/torn/conflicting recovery history")
            record = self.read(name)
            claimed = record.get("hash")
            payload = {k: v for k, v in record.items() if k != "hash"}
            if (claimed != json_digest(payload) or record.get("previous") != previous
                    or record.get("sequence") != sequence
                    or record.get("store_id") != self.header["store_id"]
                    or record.get("mailbox_key") != self.header["mailbox_key"]):
                raise CleanupError("Conflicting recovery binding/history")
            previous = claimed
            result.append(record)
        return result

    def append(self, kind, key, value):
        records = self.records()
        payload = {"sequence": len(records) + 1,
                   "previous": records[-1]["hash"] if records else json_digest(self.header),
                   "store_id": self.header["store_id"], "mailbox_key": self.header["mailbox_key"],
                   "kind": kind, "key": key, "value": value}
        payload["hash"] = json_digest(payload)
        write_private_json(self.path / f"{payload['sequence']:012d}.json", payload,
                           exclusive=True, authoritative=True)
        return payload

    def close(self):
        os.close(self.fd)
