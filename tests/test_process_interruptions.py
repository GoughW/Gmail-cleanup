"""SIGKILL boundaries cover persistence and mutation lifecycle, not power loss."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import unittest

from gmail_cleanup.engine import create,plan_message,trash_original
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.preflight import run
from .helpers import Store,message
from .process_helpers import seed,RemoteGmail,worker


class ProcessInterruptionTests(unittest.TestCase):
    def interrupt(self, mode, phase):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'state.sqlite';remote=root/'remote.json'
            original,manifest=message()
            seed(remote,original,empty_drive=mode=='preflight')
            api=RemoteGmail(remote)
            store=Store(state)
            if mode=='preflight':
                manifest={'version':2,'allowlist':['abc123'],'messages':{'abc123':[
                    {'part':s['part'],'decoded_sha256':s['decoded_sha256']} for s in manifest['messages']['abc123']]}}
            else:
                plan_message(api,store,manifest,'abc123')
                if mode=='trash':create(api,store,manifest,'abc123',execute=True)
            store.close()
            context=multiprocessing.get_context('spawn')
            parent,child=context.Pipe()
            process=context.Process(target=worker,args=(str(state),str(remote),manifest,mode,phase,child))
            process.start()
            try:
                self.assertTrue(parent.poll(20), 'Checkpoint never reached: '+phase)
                self.assertEqual(parent.recv(),phase)
                os.kill(process.pid,signal.SIGKILL)
                process.join(10)
                self.assertEqual(process.exitcode,-signal.SIGKILL)
            finally:
                if process.is_alive(): process.kill();process.join(5)
                parent.close();child.close()
            before=json.loads(remote.read_text())
            api=RemoteGmail(remote)
            store=Store(state)
            try:
                if store.recovery_error:store.restore_from_journal()
                try:
                    if mode=='preflight':run(api,store,manifest,api.drive,allow_upload=True,stop_on_failure=True)
                    elif mode=='create':create(api,store,manifest,'abc123',execute=True)
                    else:
                        record=store.get('abc123')
                        trash_original(api,store,manifest,'abc123',execute=True,enable_trash=True,
                            approve_plan=record['plan']['plan_sha256'],approve_replacement=record['replacement_id'])
                except CleanupError:
                    pass  # Ambiguous evidence correctly blocks normal execution.
                after=json.loads(remote.read_text())
                self.assertLessEqual(len(after['inserts']),1)
                self.assertLessEqual(len(after['trashes']),1)
                if before['inserts']:self.assertEqual(after['inserts'],before['inserts'])
                if before['trashes']:self.assertEqual(after['trashes'],before['trashes'])
                self.assertEqual(len(after['drive']['allocated']),len(set(after['drive']['allocated'])))
                if phase in ('after-allocation','before-upload','after-upload','after-drive-verification'):
                    self.assertEqual(after['drive']['allocated'][:len(before['drive']['allocated'])],before['drive']['allocated'])
                    if phase=='after-allocation':self.assertEqual(after['drive']['allocated'],before['drive']['allocated'])
                self.assertEqual(after['messages']['abc123']['raw_bytes'],before['messages']['abc123']['raw_bytes'])
                if mode!='trash':self.assertEqual(after['trashes'],[])
            finally:store.close()

    def test_gmail_insert_and_verification_process_boundaries(self):
        for phase in ('before-intent','after-intent','after-journal-before-db','before-insert','after-insert',
                      'before-id-persist','after-id-persist','before-replacement-verification','after-replacement-verification'):
            with self.subTest(phase=phase):self.interrupt('create',phase)

    def test_drive_allocation_upload_and_verification_process_boundaries(self):
        for phase in ('before-intent','after-intent','after-journal-before-db','before-allocation','after-allocation',
                      'before-upload','after-upload','before-drive-download','after-drive-verification'):
            with self.subTest(phase=phase):self.interrupt('preflight',phase)

    def test_trash_and_postcheck_process_boundaries(self):
        for phase in ('before-intent','after-intent','after-journal-before-db','before-trash-pending','after-trash-pending',
                      'before-trash','after-trash','before-post-trash-verification','after-post-trash-verification'):
            with self.subTest(phase=phase):self.interrupt('trash',phase)
