"""Independent history survives SQL-only rollback; ambiguous state never mutates."""
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from gmail_cleanup.engine import create, plan_message, trash_original, reconcile, mutation_binding
from gmail_cleanup.storage import Store as RealStore
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.mime import json_digest
from .helpers import Store, FakeGmail, message


def attempt_lock(path, queue):
    try:
        store = RealStore(path)
        store.close()
        queue.put('acquired')
    except CleanupError:
        queue.put('blocked')


class RecoveryJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'state.sqlite'
        self.store = Store(self.path)
        original,self.manifest = message()
        self.api = FakeGmail({'abc123':original})
        self.record = plan_message(self.api,self.store,self.manifest,'abc123')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def snapshot(self):
        path = self.root/'snapshot.sqlite'
        connection=sqlite3.connect(path)
        self.store.db.backup(connection)
        connection.close()
        return path

    def restore(self,path):
        self.store.close()
        source=sqlite3.connect(path)
        destination=sqlite3.connect(self.path)
        source.backup(destination)
        source.close()
        destination.close()
        self.store=RealStore(self.path)

    def test_planned_restore_after_accepted_insert_lost_response_never_reinserts(self):
        snapshot=self.snapshot()
        self.api.lose_insert_response=True
        with self.assertRaises(CleanupError):
            create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.assertEqual(len(self.api.inserts),1)
        self.restore(snapshot)
        self.assertIsNotNone(self.store.recovery_error)
        with self.assertRaises(CleanupError):
            create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.assertEqual(len(self.api.inserts),1)
        self.store.restore_from_journal()
        self.assertEqual(self.store.get('abc123')['state'],'insert_pending')
        with self.assertRaises(CleanupError):
            create(self.api,self.store,self.manifest,'abc123',execute=True)
        recovered=reconcile(self.api,self.store,self.manifest,'abc123',self.api.inserts[0],execute=True)
        self.assertEqual(recovered['state'],'verified')
        self.assertEqual(len(self.api.inserts),1)
        self.assertEqual(self.api.trashes,[])

    def test_intent_before_pending_snapshot_still_prevents_insert_after_local_recovery(self):
        self.store.intent('gmail-insert',mutation_binding(self.record))
        self.store.close()
        self.store=RealStore(self.path)
        with self.assertRaises(CleanupError):
            create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.assertEqual(self.api.inserts,[])
        self.store.restore_from_journal()
        self.assertEqual(self.store.get('abc123')['state'],'insert_pending')

    def test_mailbox_and_plan_binding_conflicts_block_before_mutation(self):
        self.api.mailbox_identity=lambda:'different-account'
        with self.assertRaises(CleanupError):
            create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.assertEqual(self.api.inserts+self.api.trashes,[])
        wrong=deepcopy(self.record)
        wrong['plan']['operation_id']='changed'
        with self.assertRaises(CleanupError):
            self.store.save(wrong,expected_state='planned')

    def test_two_processes_cannot_hold_lifecycle_lock(self):
        queue=multiprocessing.Queue()
        process=multiprocessing.Process(target=attempt_lock,args=(str(self.path),queue))
        process.start();process.join(10)
        self.assertEqual(process.exitcode,0)
        self.assertEqual(queue.get(timeout=2),'blocked')

    def test_missing_torn_older_or_conflicting_journal_blocks_all_mutations(self):
        self.store.close()
        self.store=None
        journal=Path(str(self.path)+'.recovery')
        saved={p.name:p.read_bytes() for p in journal.iterdir()}
        for defect in ('missing','torn','older','conflict'):
            with self.subTest(defect=defect):
                for p in journal.iterdir():p.unlink()
                for name,data in saved.items(): (journal/name).write_bytes(data);(journal/name).chmod(0o600)
                entry=journal/'000000000001.json'
                if defect=='missing': (journal/'genesis.json').unlink()
                if defect=='torn': entry.write_text('{')
                if defect=='older': entry.unlink()
                if defect=='conflict':
                    row=json.loads(entry.read_text());row['value']['state']='verified';entry.write_text(json.dumps(row))
                store=RealStore(self.path)
                try:
                    with self.assertRaises(CleanupError):create(self.api,store,self.manifest,'abc123',execute=True)
                finally:store.close()
                self.assertEqual(self.api.inserts+self.api.trashes,[])
        # Restore evidence for teardown without touching remote fake state.
        for p in journal.iterdir():p.unlink()
        for name,data in saved.items(): (journal/name).write_bytes(data);(journal/name).chmod(0o600)
        self.store=RealStore(self.path)

    def test_zero_multiple_and_delayed_candidates_never_clear_uncertainty(self):
        self.api.lose_insert_response=True
        with self.assertRaises(CleanupError):create(self.api,self.store,self.manifest,'abc123',execute=True)
        for ids in ([],['replacement1','replacement2'],['not-visible-yet']):
            with self.subTest(ids=ids),self.assertRaises(CleanupError):
                reconcile(self.api,self.store,self.manifest,'abc123',ids,execute=True)
        self.assertEqual(self.store.get('abc123')['state'],'insert_pending')
        self.assertEqual(len(self.api.inserts),1)
        self.assertEqual(self.api.trashes,[])

    def test_legacy_inspection_and_candidate_recording_never_grant_trash(self):
        record=create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.store.close()
        # Emulate an immutable pre-policy database using a closed copy. No rewrite
        # occurs in production recovery; test setup constructs the historical input.
        legacy=deepcopy(record)
        legacy['plan'].pop('safety_policy');legacy['plan'].pop('transformation_policy')
        plan=deepcopy(legacy['plan']);plan.pop('plan_sha256');legacy['plan']['plan_sha256']=json_digest(plan)
        con=sqlite3.connect(self.path)
        con.execute('UPDATE operations SET record=? WHERE message_id=?',(json.dumps(legacy),'abc123'));con.commit();con.close()
        self.store=RealStore(self.path)
        before=deepcopy(self.store.get('abc123'))
        observed=reconcile(self.api,self.store,self.manifest,'abc123','replacement1',execute=True)
        self.assertEqual(observed['state'],'recovery_only')
        self.assertFalse(observed['verification']['current_policy_eligible'])
        self.assertEqual(self.store.get('abc123'),before)
        with self.assertRaises(CleanupError):
            trash_original(self.api,self.store,self.manifest,'abc123',execute=True,enable_trash=True,
                approve_plan=legacy['plan']['plan_sha256'],approve_replacement='replacement1')
        self.assertEqual(self.api.trashes,[])
