"""R2: direction, ancestry and EVERY surviving SQL evidence source before writes."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import multiprocessing
import os
import signal
import tempfile
import unittest
from unittest.mock import patch

from gmail_cleanup.cli import main
from gmail_cleanup.drive import Backups
from gmail_cleanup.engine import create, plan_message, trash_original, mutation_binding
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.mime import json_digest
from gmail_cleanup.storage import Store as ProductionStore
from .helpers import Store, FakeGmail, message
from .test_rereview_regressions import capture_journal, replace_journal, capture_sql


def interrupted_local_recovery(path, pipe):
    store=ProductionStore(path)
    append=store.journal.append
    def published(*args):
        result=append(*args)
        pipe.send('recovery-journal-published-before-sql-commit')
        pipe.recv()
        return result
    store.journal.append=published
    try:store.restore_from_journal()
    finally:store.close()


class RecoveryDirectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name);self.path=self.root/'state.sqlite'
        original,self.manifest=message()
        self.api=FakeGmail({'abc123':original})
        self.store=Store(self.path)
        self.record=plan_message(self.api,self.store,self.manifest,'abc123')
        self.prefix=capture_journal(self.store.journal_path)

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def reopen(self, candidate=None):
        journal=self.store.journal_path
        self.store.close()
        if candidate is not None:replace_journal(journal,candidate)
        self.store=ProductionStore(self.path)

    def assert_rejected_unchanged(self):
        before_sql=capture_sql(self.store)
        before_file=self.path.read_bytes()
        before_journal=capture_journal(self.store.journal_path)
        writes=(list(self.api.inserts),list(self.api.trashes),list(self.api.drive.allocated),list(self.api.drive.creates))
        with self.assertRaises(CleanupError):self.store.restore_from_journal()
        self.assertIsNotNone(self.store.recovery_error)
        self.assertEqual(capture_sql(self.store),before_sql)
        self.assertEqual(self.path.read_bytes(),before_file)
        self.assertEqual(capture_journal(self.store.journal_path),before_journal)
        with self.assertRaises(CleanupError):self.store.require_current(self.api)
        self.assertEqual(writes,(self.api.inserts,self.api.trashes,self.api.drive.allocated,self.api.drive.creates))

    def snapshot(self):
        path=self.root/'snapshot.sqlite';con=sqlite3.connect(path)
        self.store.db.backup(con);con.close()
        return path

    def restore_sql(self,path):
        self.store.close()
        source=sqlite3.connect(path);destination=sqlite3.connect(self.path)
        source.backup(destination);source.close();destination.close()
        self.store=ProductionStore(self.path)

    def rechain(self, events, header=None):
        header=header or json.loads(self.prefix['genesis.json'])
        previous=json_digest(header)
        candidate={'genesis.json':json.dumps(header).encode()}
        for sequence,event in enumerate(events,1):
            row={**event,'sequence':sequence,'previous':previous,
                 'store_id':header['store_id'],'mailbox_key':header['mailbox_key']}
            row.pop('hash',None);row['hash']=json_digest(row)
            previous=row['hash'];candidate[f'{sequence:012d}.json']=json.dumps(row).encode()
        return candidate

    def test_equal_length_divergence_and_longer_incompatible_hash_ancestry(self):
        event=json.loads(self.prefix['000000000001.json'])
        changed=deepcopy(event);changed['value']['state']='insert_pending'
        for events in ([changed],[changed,event]):
            with self.subTest(length=len(events)):
                self.reopen(self.rechain(events));self.assert_rejected_unchanged()
        self.reopen(self.prefix)

    def test_different_store_for_same_mailbox_is_not_a_compatible_recovery_source(self):
        header=json.loads(self.prefix['genesis.json']);header['store_id']='f'*32
        event=json.loads(self.prefix['000000000001.json'])
        self.reopen(self.rechain([event],header));self.assert_rejected_unchanged()

    def test_different_mailbox_with_same_store_id_is_not_compatible(self):
        header=json.loads(self.prefix['genesis.json']);header['mailbox_key']='different-account'
        event=json.loads(self.prefix['000000000001.json'])
        self.reopen(self.rechain([event],header));self.assert_rejected_unchanged()

    def test_sql_only_intent_and_conflicting_latest_operation_block_recovery(self):
        before=capture_sql(self.store)
        intent={'action':'gmail-insert','binding':{'mailbox_key':'fake-mailbox',**mutation_binding(self.record)},'state':'pending'}
        self.store.db.execute('INSERT INTO intents VALUES (?,?)',('SQL_ONLY',json.dumps(intent)))
        self.reopen();self.assert_rejected_unchanged()
        self.store.db.execute("DELETE FROM intents WHERE intent_key='SQL_ONLY'")
        self.store.db.execute('INSERT INTO backups VALUES (?,?)',('SQL_ONLY',json.dumps({'state':'upload_pending','file_id':'retained-SQL-ID'})))
        self.reopen();self.assert_rejected_unchanged()
        self.store.db.execute("DELETE FROM backups WHERE backup_key='SQL_ONLY'")
        changed=deepcopy(self.record);changed['state']='insert_pending'
        self.store.db.execute('UPDATE operations SET record=?',(json.dumps(changed),))
        self.reopen();self.assert_rejected_unchanged()
        self.store.db.execute('UPDATE operations SET record=?',(before['operations'][0][1],))

    def test_surviving_sql_event_cannot_be_ignored_even_when_head_and_latest_maps_match(self):
        for table in ('events','backup_events'):
            with self.subTest(table=table):
                event=deepcopy(self.record);event['state']='insert_pending';event['insert_intent_key']='surviving-sql-only-evidence'
                self.store.db.execute(f'INSERT INTO {table} VALUES (NULL,?,?,?)',('abc123','2026-10-14T00:00:00+00:00',json.dumps(event)))
                self.reopen();self.assert_rejected_unchanged()
                self.store.db.execute(f'DELETE FROM {table} WHERE seq=(SELECT MAX(seq) FROM {table})')

    def test_missing_malformed_or_duplicated_metadata_blocks_before_writes(self):
        original=self.store.db.execute('SELECT record FROM recovery_meta').fetchone()[0]
        head=json.loads(original)
        variants=[None,'null','[]','{',json.dumps({**head,'sequence':True}),json.dumps({**head,'sequence':-1}),
                  json.dumps({**head,'hash':'not-a-hash'}),json.dumps({**head,'sequence':float('nan')}),
                  json.dumps({**head,'store_id':123}),original[:-1]+',"sequence":1}']
        for encoded in variants:
            with self.subTest(encoded=encoded):
                self.store.db.execute('DELETE FROM recovery_meta')
                if encoded is not None:self.store.db.execute('INSERT INTO recovery_meta VALUES (1,?)',(encoded,))
                self.reopen();self.assert_rejected_unchanged()
        self.store.db.execute('DELETE FROM recovery_meta')
        self.store.db.execute('INSERT INTO recovery_meta VALUES (1,?)',(original,))

    def test_malformed_unacknowledged_suffix_blocks_before_any_recovery_write(self):
        first=json.loads(self.prefix['000000000001.json'])
        variants=[{'kind':'backups','key':'bad-backup','value':{'state':'upload_pending'}},
                  {'kind':'operations','key':'unknown-message','value':{'state':'planned'}},
                  {'kind':'intents','key':'bad-intent','value':{'state':'pending','action':'gmail-insert'}},
                  {'kind':'backups','key':'bad-backup','value':{'state':'stored','file_id':'fake-ID','invalid':float('nan')}}]
        binding={'mailbox_key':'fake-mailbox',**mutation_binding(self.record),'backup_key':'bad-backup','name':'file.bin'}
        allocation={'state':'observed','action':'drive-allocate','binding':binding,'result':None}
        variants.append({'kind':'intents','key':json_digest({'action':'drive-allocate','binding':binding}),'value':allocation})
        for extra in variants:
            with self.subTest(kind=extra['kind']):
                self.reopen(self.rechain([first,extra]));self.assert_rejected_unchanged()
        extra={'kind':'backups','key':'bad-backup','value':{'state':'stored','file_id':'fake-ID'}}
        candidate=self.rechain([first,extra])
        candidate['000000000002.json']=candidate['000000000002.json'].replace(
            b'"state": "stored"',b'"state": "upload_pending", "state": "stored"')
        self.reopen(candidate);self.assert_rejected_unchanged()
        self.reopen(self.prefix)

    def test_sql_ahead_known_replacement_cannot_be_erased_by_older_journal(self):
        record=create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.reopen(self.prefix);self.assert_rejected_unchanged()
        self.assertEqual(self.store.get('abc123')['replacement_id'],record['replacement_id'])
        inspected=create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.assertFalse(inspected['verification']['current_policy_eligible'])
        self.assertEqual(self.api.inserts,['replacement1'])

    def test_compatible_hash_extension_cannot_retract_known_identity_or_plan(self):
        record=create(self.api,self.store,self.manifest,'abc123',execute=True)
        saved=capture_journal(self.store.journal_path)
        events=[json.loads(data) for name,data in sorted(saved.items()) if name!='genesis.json']
        for field,value in (('replacement_id','replacement2'),('insert_intent_key','other-intent'),('plan',{})):
            with self.subTest(field=field):
                changed=deepcopy(record);changed[field]=value
                extra={'kind':'operations','key':'abc123','value':changed}
                self.reopen(self.rechain(events+[extra]));self.assert_rejected_unchanged()
        self.reopen(saved)

    def prepare_uncertain_upload(self):
        self.store.context=mutation_binding(self.record)
        drive=self.api.drive;drive.lose_response=True
        get=drive.get
        drive.get=lambda file_id:None if file_id in drive.creates else get(file_id)
        with self.assertRaises(CleanupError):
            Backups(drive,self.store)._create_once('uncertain-backup','file.bin','fixture-folder',b'backup-bytes',True)
        return get,drive.allocated[0]

    def test_sql_ahead_uncertain_drive_upload_keeps_id_and_never_allocates_again(self):
        _,file_id=self.prepare_uncertain_upload()
        self.reopen(self.prefix);self.assert_rejected_unchanged()
        self.assertEqual(self.store.get_backup('uncertain-backup')['file_id'],file_id)
        with self.assertRaises(CleanupError):Backups(self.api.drive,self.store)._create_once('uncertain-backup','file.bin','fixture-folder',b'backup-bytes',True)
        self.assertEqual(self.api.drive.allocated,[file_id]);self.assertEqual(self.api.drive.creates,[file_id])

    def test_uncertain_allocation_evidence_cannot_be_cleared_by_journal_rollback(self):
        self.store.context=mutation_binding(self.record)
        allocate=self.api.drive.allocate_id
        def lost():allocate();raise CleanupError('Fake allocation response lost')
        self.api.drive.allocate_id=lost
        with self.assertRaises(CleanupError):Backups(self.api.drive,self.store)._create_once('uncertain-allocation','file.bin','fixture-folder',b'backup-bytes',True)
        self.reopen(self.prefix);self.assert_rejected_unchanged()
        self.api.drive.allocate_id=allocate
        with self.assertRaises(CleanupError):Backups(self.api.drive,self.store)._create_once('uncertain-allocation','file.bin','fixture-folder',b'backup-bytes',True)
        self.assertEqual(len(self.api.drive.allocated),1);self.assertEqual(self.api.drive.creates,[])

    def test_sql_ahead_pending_or_completed_trash_retains_approval_and_blocks_replay(self):
        for lost in (True,False):
            with self.subTest(lost=lost):
                if self.store.get('abc123')['state']!='planned':
                    # Independent temporary stores per case; no live data migration.
                    self.store.close();self.temp.cleanup();self.setUp()
                record=create(self.api,self.store,self.manifest,'abc123',execute=True)
                prefix=capture_journal(self.store.journal_path)
                self.api.lose_trash_response=lost
                try:trash_original(self.api,self.store,self.manifest,'abc123',execute=True,enable_trash=True,
                    approve_plan=record['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
                except CleanupError:pass
                approval=deepcopy(self.store.get('abc123')['approval'])
                self.reopen(prefix);self.assert_rejected_unchanged()
                self.assertEqual(self.store.get('abc123')['approval'],approval)
                with self.assertRaises(CleanupError):trash_original(self.api,self.store,self.manifest,'abc123',execute=True,enable_trash=True,
                    approve_plan=approval['plan_sha256'],approve_replacement=approval['replacement_id'])
                self.assertEqual(self.api.trashes,['abc123'])

    def test_positive_sql_behind_known_replacement_recovery_is_repeatable_and_reuses_id(self):
        snapshot=self.snapshot();created=create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.restore_sql(snapshot)
        self.assertIsNotNone(self.store.recovery_error)
        for _ in range(2):self.store.restore_from_journal()
        recovered=self.store.get('abc123')
        self.assertEqual(recovered['plan'],created['plan'])
        self.assertEqual(recovered['replacement_id'],created['replacement_id'])
        self.assertEqual(create(self.api,self.store,self.manifest,'abc123',execute=True)['state'],'verified')
        self.assertEqual(self.api.inserts,['replacement1']);self.assertEqual(self.api.trashes,[])

    def test_positive_sql_behind_uncertain_upload_preserves_id_and_intent(self):
        snapshot=self.snapshot();get,file_id=self.prepare_uncertain_upload()
        self.restore_sql(snapshot)
        for _ in range(2):self.store.restore_from_journal()
        self.assertEqual(self.store.get_backup('uncertain-backup')['file_id'],file_id)
        with self.assertRaises(CleanupError):Backups(self.api.drive,self.store)._create_once('uncertain-backup','file.bin','fixture-folder',b'backup-bytes',True)
        self.api.drive.get=get
        self.assertEqual(Backups(self.api.drive,self.store)._create_once('uncertain-backup','file.bin','fixture-folder',b'backup-bytes',True),file_id)
        self.assertEqual(self.api.drive.allocated,[file_id]);self.assertEqual(self.api.drive.creates,[file_id])

    def test_valid_ancestry_cannot_retract_allocated_drive_id_or_observed_result(self):
        self.prepare_uncertain_upload()
        saved=capture_journal(self.store.journal_path)
        events=[json.loads(data) for name,data in sorted(saved.items()) if name!='genesis.json']
        backup=deepcopy(self.store.get_backup('uncertain-backup'));backup['file_id']='different-ID'
        allocation=next(row for row in events if row['kind']=='intents' and row['value']['action']=='drive-allocate' and row['value']['state']=='observed')
        changed=deepcopy(allocation['value']);changed['result']['file_id']='different-ID'
        for extra in ({'kind':'backups','key':'uncertain-backup','value':backup},
                      {'kind':'intents','key':allocation['key'],'value':changed}):
            with self.subTest(kind=extra['kind']):
                self.reopen(self.rechain(events+[extra]));self.assert_rejected_unchanged()
        self.reopen(saved)

    def test_positive_sql_behind_pending_and_completed_trash_keeps_exact_approval(self):
        for lost in (True,False):
            with self.subTest(lost=lost):
                if self.store.get('abc123')['state']!='planned':
                    self.store.close();self.temp.cleanup();self.setUp()
                record=create(self.api,self.store,self.manifest,'abc123',execute=True)
                snapshot=self.snapshot()
                self.api.lose_trash_response=lost
                try:trash_original(self.api,self.store,self.manifest,'abc123',execute=True,enable_trash=True,
                    approve_plan=record['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
                except CleanupError:pass
                expected=deepcopy(self.store.get('abc123'))
                self.restore_sql(snapshot)
                for _ in range(2):self.store.restore_from_journal()
                recovered=self.store.get('abc123')
                self.assertEqual(recovered['approval'],expected['approval'])
                self.assertEqual(recovered['plan'],expected['plan'])
                self.assertEqual(recovered['replacement_id'],expected['replacement_id'])
                result=trash_original(self.api,self.store,self.manifest,'abc123',execute=True,enable_trash=True,
                    approve_plan=recovered['approval']['plan_sha256'],approve_replacement=recovered['approval']['replacement_id'])
                self.assertEqual(result['state'],'trashed')
                self.assertEqual(self.api.trashes,['abc123']);self.assertEqual(self.api.inserts,['replacement1'])

    def test_sigkill_during_recovery_append_preserves_compatible_prefix_and_uncertainty(self):
        snapshot=self.snapshot()
        self.store.intent('gmail-insert',mutation_binding(self.record))
        self.restore_sql(snapshot);self.store.close()
        context=multiprocessing.get_context('spawn');parent,child=context.Pipe()
        process=context.Process(target=interrupted_local_recovery,args=(str(self.path),child))
        process.start()
        try:
            self.assertTrue(parent.poll(20))
            self.assertEqual(parent.recv(),'recovery-journal-published-before-sql-commit')
            os.kill(process.pid,signal.SIGKILL);process.join(10)
            self.assertEqual(process.exitcode,-signal.SIGKILL)
        finally:
            if process.is_alive():process.kill();process.join(5)
            parent.close();child.close()
            self.store=ProductionStore(self.path)
        self.assertIsNotNone(self.store.recovery_error)
        for _ in range(2):self.store.restore_from_journal()
        recovered=self.store.get('abc123')
        self.assertEqual(recovered['state'],'insert_pending');self.assertEqual(recovered['plan'],self.record['plan'])
        with self.assertRaises(CleanupError):create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.assertEqual(self.api.inserts+self.api.trashes+self.api.drive.allocated+self.api.drive.creates,[])

    def test_journal_publication_before_sql_commit_is_compatible_but_not_permission_to_insert(self):
        append=self.store.journal.append
        def interrupted(*args):append(*args);raise RuntimeError('Injected interruption after durable append')
        with patch.object(self.store.journal,'append',side_effect=interrupted),self.assertRaises(RuntimeError):
            self.store.intent('gmail-insert',mutation_binding(self.record))
        self.reopen()
        for _ in range(2):self.store.restore_from_journal()
        self.assertEqual(self.store.get('abc123')['state'],'insert_pending')
        with self.assertRaises(CleanupError):create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.assertEqual(self.api.inserts+self.api.trashes+self.api.drive.allocated+self.api.drive.creates,[])

    def test_local_recovery_cli_rejects_stale_history_without_auth_or_evidence_changes(self):
        self.api.lose_insert_response=True
        with self.assertRaises(CleanupError):create(self.api,self.store,self.manifest,'abc123',execute=True)
        self.reopen(self.prefix)
        before_sql=capture_sql(self.store);before_journal=capture_journal(self.store.journal_path)
        self.store.close()
        with patch('gmail_cleanup.cli.backend') as backend,redirect_stdout(io.StringIO()) as output:
            code=main(['recover-state','--state',str(self.path),'--mailbox-key','fake-mailbox'])
        self.assertEqual(code,2);backend.assert_not_called()
        self.assertIn('older than acknowledged',json.loads(output.getvalue())['error'])
        self.store=ProductionStore(self.path)
        self.assertEqual(capture_sql(self.store),before_sql)
        self.assertEqual(capture_journal(self.store.journal_path),before_journal)
        self.assertEqual(self.api.inserts,['replacement1'])
