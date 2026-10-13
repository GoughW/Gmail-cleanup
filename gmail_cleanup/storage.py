"""Private operation/backup audit; never stores raw mail, addresses or tokens."""

import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from .errors import CleanupError
from .paths import ACTIVE, PathSession, durable_parents


def private_parent(path):
    # Do not resolve a final symlink before opening with O_NOFOLLOW.
    path = Path(path).expanduser().absolute()
    durable_parents(path.parent)
    return path


def write_private_json(path, data, exclusive=False, authoritative=False):
    # Serialization cannot destroy the last valid artifact.
    encoded = (json.dumps(data, indent=2, ensure_ascii=True) + "\n").encode()
    # Only internal credential/journal writers may target their own protected path.
    # User-selected reports always use the pre-authentication CLI path session.
    session = None if authoritative else ACTIVE.get()
    own = session is None
    if own:
        session = PathSession()
    target = session.target(path)
    temporary = None
    try:
        target.check()
        temporary = ".artifact-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target.fd)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            written = stream.write(encoded)
            if written != len(encoded):
                raise OSError("Short artifact write")
            stream.flush()
            os.fsync(stream.fileno())
        target.check()
        if exclusive:
            # Link publishes the complete inode with true no-clobber semantics.
            os.link(temporary, target.name, src_dir_fd=target.fd, dst_dir_fd=target.fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=target.fd)
        else:
            os.replace(temporary, target.name, src_dir_fd=target.fd, dst_dir_fd=target.fd)
        temporary = None
        target.advance()
        try:
            os.fsync(target.fd)
        except OSError:
            raise CleanupError("Artifact replaced, but directory durability is uncertain") from None
    finally:
        if temporary:
            try:
                os.unlink(temporary, dir_fd=target.fd)
            except FileNotFoundError:
                pass
        if own:
            session.close()


class Store:
    """SQLite plus independent write-once journal; one exclusive lifecycle lock."""
    def __init__(self, path, *, initialize=False, mailbox_key=None, journal=None):
        import fcntl
        from .paths import Target
        from .recovery import Journal
        self.path = private_parent(path)
        self.target = Target(self.path)
        self.db, self.journal, self.lock_fd = None, None, None
        self.context = {}
        self.recovery_error = None
        self.mailbox_key = mailbox_key
        self.journal_path = Path(journal or (str(self.path) + '.recovery')).absolute()
        try:
            self.lock_fd = os.open(self.path.name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                   0o600, dir_fd=self.target.fd)
            info = os.fstat(self.lock_fd)
            if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise CleanupError('Unsafe state lock file')
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CleanupError('Another operator holds this state lock') from None
            self.target.check()
            exists = self.path.exists()
            if initialize:
                if exists or self.journal_path.exists() or not mailbox_key:
                    raise CleanupError('Initialization requires a new state/journal and explicit mailbox key')
            elif not exists:
                raise CleanupError('Missing state: explicit initialization or recovery review required')
            flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if initialize else 0)
            fd = os.open(self.path.name, flags, 0o600, dir_fd=self.target.fd)
            info = os.fstat(fd)
            os.close(fd)
            if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise CleanupError('State must be private, regular and singly linked')
            self.target.advance()
            self.db = sqlite3.connect(f'/proc/self/fd/{self.target.fd}/{self.path.name}', timeout=5, isolation_level=None)
            self.target.check()
            self.db.execute('PRAGMA synchronous=FULL')
            if initialize:
                self.journal = Journal(self.journal_path, initialize=True, mailbox_key=mailbox_key)
                self.db.executescript("""
                    CREATE TABLE operations (message_id TEXT PRIMARY KEY, record TEXT NOT NULL);
                    CREATE TABLE events (seq INTEGER PRIMARY KEY, message_id TEXT NOT NULL, time TEXT NOT NULL, record TEXT NOT NULL);
                    CREATE TABLE backups (backup_key TEXT PRIMARY KEY, record TEXT NOT NULL);
                    CREATE TABLE backup_events (seq INTEGER PRIMARY KEY, backup_key TEXT NOT NULL, time TEXT NOT NULL, record TEXT NOT NULL);
                    CREATE TABLE intents (intent_key TEXT PRIMARY KEY, record TEXT NOT NULL);
                    CREATE TABLE recovery_meta (id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL);
                """)
                self._set_head([])
                os.fsync(self.target.fd)
            else:
                try:
                    self.journal = Journal(self.journal_path)
                    if mailbox_key and mailbox_key != self.journal.header['mailbox_key']:
                        raise CleanupError('Configured mailbox conflicts with recovery history')
                    self.mailbox_key = self.journal.header['mailbox_key']
                    self.check_history()
                except (CleanupError, OSError, sqlite3.Error) as exc:
                    self.recovery_error = str(exc) if isinstance(exc, CleanupError) else 'Missing/incomplete recovery evidence'
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.journal is not None:
            self.journal.close()
            self.journal = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if getattr(self, 'target', None) is not None:
            self.target.close()
            self.target = None

    def _maps(self, records):
        maps = {'operations': {}, 'backups': {}, 'intents': {}}
        for event in records:
            if event['kind'] not in maps:
                raise CleanupError('Unknown authoritative journal event')
            maps[event['kind']][event['key']] = deepcopy(event['value'])
        return maps

    def _set_head(self, records):
        from .mime import json_digest
        head = {'store_id': self.journal.header['store_id'], 'mailbox_key': self.mailbox_key,
                'sequence': len(records), 'hash': records[-1]['hash'] if records else json_digest(self.journal.header)}
        self.db.execute('INSERT OR REPLACE INTO recovery_meta VALUES (1,?)', (json.dumps(head, sort_keys=True),))

    def check_history(self):
        from .mime import json_digest
        self.target.check()
        if self.journal is None:
            raise CleanupError('Missing journal: legacy/restored state is recovery-only')
        records = self.journal.records()
        expected = {'store_id': self.journal.header['store_id'], 'mailbox_key': self.journal.header['mailbox_key'],
                    'sequence': len(records), 'hash': records[-1]['hash'] if records else json_digest(self.journal.header)}
        row = self.db.execute('SELECT record FROM recovery_meta WHERE id=1').fetchone()
        if not row or json.loads(row[0]) != expected:
            raise CleanupError('Journal/database history differs: restore recovery required')
        for table, wanted in self._maps(records).items():
            observed = {key: json.loads(value) for key, value in self.db.execute(f'SELECT * FROM {table}')}
            if observed != wanted:
                raise CleanupError('Conflicting database/journal records: recovery required')
        return records

    def require_current(self, api=None):
        if self.recovery_error:
            raise CleanupError(self.recovery_error)
        self.check_history()
        if api is not None:
            actual = api.mailbox_identity()
            if actual != self.mailbox_key:
                raise CleanupError('Authenticated mailbox does not match authoritative state')

    def get(self, message_id):
        self.target.check()
        row = self.db.execute('SELECT record FROM operations WHERE message_id=?', (message_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_backup(self, key):
        self.target.check()
        row = self.db.execute('SELECT record FROM backups WHERE backup_key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, table, key, record, expected_state=None, new=False):
        self.require_current()
        getter = self.get if table == 'operations' else self.get_backup
        current = getter(key) if table != 'intents' else self.get_intent(key)
        if new and current is not None:
            raise CleanupError('Existing authoritative record; inspect before retrying')
        if expected_state is not None and (current is None or current['state'] != expected_state):
            raise CleanupError('State changed; inspect before continuing')
        if table == 'operations' and current is not None:
            if current['plan'] != record['plan'] or (current.get('approval') and current['approval'] != record.get('approval')):
                raise CleanupError('Saved plans and approvals are immutable')
        # Journal first. A crash before DB commit leaves recovery-only evidence.
        event = self.journal.append(table, key, record)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            encoded = json.dumps(record, sort_keys=True)
            self.db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?)', (key, encoded))
            if table in ('operations', 'backups'):
                events = 'events' if table == 'operations' else 'backup_events'
                self.db.execute(f'INSERT INTO {events} VALUES (NULL,?,?,?)',
                                (key, datetime.now(timezone.utc).isoformat(), encoded))
            self._set_head(self.journal.records())
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            self.recovery_error = 'Journal ahead of database: recovery required'
            raise

    def save(self, record, expected_state=None, new=False):
        self._save('operations', record['message_id'], record, expected_state, new)

    def save_backup(self, key, record, expected_state=None, new=False):
        self._save('backups', key, record, expected_state, new)

    def get_intent(self, key):
        row = self.db.execute('SELECT record FROM intents WHERE intent_key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def intent(self, action, binding):
        from .mime import json_digest
        self.require_current()
        binding = {'mailbox_key': self.mailbox_key, **self.context, **binding}
        if binding['mailbox_key'] != self.mailbox_key:
            raise CleanupError('Mutation mailbox binding conflicts')
        required = {'mailbox_key', 'original_id', 'operation_id', 'plan_sha256'}
        if not required.issubset(binding) or any(not binding[k] for k in required):
            raise CleanupError('Incomplete mutation identity/plan binding')
        for _, encoded in self.db.execute('SELECT * FROM intents'):
            old = json.loads(encoded)
            subject = 'backup_key' if action.startswith('drive-') else 'original_id'
            if old['action'] == action and old['binding'].get(subject) == binding.get(subject):
                raise CleanupError('Prior mutation evidence exists for this identity; explicit recovery required')
        key = json_digest({'action': action, 'binding': binding})
        if self.get_intent(key):
            raise CleanupError('Mutation may already have been submitted; recovery only')
        record = {'action': action, 'binding': binding, 'state': 'pending'}
        self._save('intents', key, record, new=True)
        return key

    def allocation_for(self, backup_key):
        self.require_current()
        matches = [json.loads(value) for _, value in self.db.execute('SELECT * FROM intents')
                   if json.loads(value)['action'] == 'drive-allocate'
                   and json.loads(value)['binding'].get('backup_key') == backup_key]
        if len(matches) > 1:
            raise CleanupError('Ambiguous allocation history')
        return matches[0] if matches else None

    def complete_intent(self, key, result):
        record = self.get_intent(key)
        if not record or record['state'] != 'pending':
            raise CleanupError('Missing/conflicting mutation intent')
        record.update(state='observed', result=result)
        self._save('intents', key, record, expected_state='pending')

    def restore_from_journal(self):
        # Explicit LOCAL recovery only. Preserve attempted actions, even if the
        # last operation snapshot predates recording its pending lifecycle state.
        if self.journal is None:
            raise CleanupError('No authoritative journal; independent recovery review required')
        if self.mailbox_key not in (None, self.journal.header['mailbox_key']):
            raise CleanupError('Configured mailbox conflicts; recovery cannot overwrite this state')
        records = self.journal.records()
        maps = self._maps(records)
        for intent_key, intent in maps['intents'].items():
            binding = intent['binding']
            if intent['action'] in ('gmail-insert', 'gmail-trash'):
                operation = maps['operations'].get(binding['original_id'])
                if (not operation or operation['plan']['operation_id'] != binding['operation_id']
                        or operation['plan']['plan_sha256'] != binding['plan_sha256']):
                    raise CleanupError('Mutation intent conflicts with approved operation')
                if intent['action'] == 'gmail-insert' and not operation.get('replacement_id'):
                    operation['state'] = 'insert_pending'
                    operation['insert_intent_key'] = intent_key
                if intent['action'] == 'gmail-trash':
                    operation['state'] = 'trash_pending'
                    operation['trash_intent_key'] = intent_key
                    operation['approval'] = binding['approval']
            if intent['action'] == 'drive-allocate' and intent.get('result', {}).get('file_id'):
                maps['backups'].setdefault(binding['backup_key'], {'state': 'upload_pending',
                    'file_id': intent['result']['file_id'], 'expected_name': binding['name']})
        # Recovery decisions themselves become new durable history, never erase it.
        for table in maps:
            for key, value in maps[table].items():
                if value != self._maps(records)[table].get(key):
                    self.journal.append(table, key, value)
        records = self.journal.records()
        maps = self._maps(records)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            for table, values in maps.items():
                self.db.execute(f'DELETE FROM {table}')
                self.db.executemany(f'INSERT INTO {table} VALUES (?,?)', [(k, json.dumps(v, sort_keys=True)) for k,v in values.items()])
            self._set_head(records)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        self.recovery_error = None
        self.check_history()

    def record_recovery(self, record):
        # Inspection notes are not execution authority and do not rewrite legacy DBs.
        notes = Path(str(self.path) + '.inspection')
        notes.mkdir(mode=0o700, exist_ok=True)
        write_private_json(notes / (uuid.uuid4().hex + '.json'), record,
                           exclusive=True, authoritative=True)
