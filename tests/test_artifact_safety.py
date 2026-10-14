from contextlib import redirect_stdout
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gmail_cleanup.cli import main
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.paths import PathSession, ACTIVE
from gmail_cleanup.storage import Store, write_private_json


def exclusive_creator(path, value, queue):
    try:
        write_private_json(path, {"winner": value}, exclusive=True)
        queue.put("won")
    except FileExistsError:
        queue.put("lost")


def interrupted_replace(path, phase, pipe):
    real_replace=os.replace
    def replace(*args, **kwargs):
        if phase=='before':pipe.send(phase);pipe.recv()
        result=real_replace(*args,**kwargs)
        if phase=='after':pipe.send(phase);pipe.recv()
        return result
    with patch('gmail_cleanup.storage.os.replace',side_effect=replace):
        write_private_json(path,{'new':True})


class ArtifactSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "report.json"
        write_private_json(self.path, {"old": True})
        self.before = self.path.read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def test_serialization_failure_preserves_previous_artifact(self):
        with self.assertRaises(TypeError):
            write_private_json(self.path, {"invalid": object()})
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_failed_write_and_file_fsync_preserve_old_bytes(self):
        for operation in ("os.fdopen", "os.fsync", "os.replace"):
            with self.subTest(operation=operation), patch("gmail_cleanup.storage." + operation, side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_private_json(self.path, {"new": True})
            self.assertEqual(self.path.read_bytes(), self.before)
            self.assertEqual(list(self.root.glob(".artifact-*")), [])

    def test_directory_fsync_failure_reports_new_complete_but_uncertain_durability(self):
        original = os.fsync
        calls = []
        def fsync(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("directory failure")
            original(fd)
        with patch("gmail_cleanup.storage.os.fsync", side_effect=fsync), self.assertRaisesRegex(CleanupError, "durability is uncertain"):
            write_private_json(self.path, {"new": True})
        self.assertEqual(json.loads(self.path.read_bytes()), {"new": True})

    def test_final_symlinks_and_hardlinks_are_never_write_targets(self):
        for kind in ("symlink", "hardlink"):
            alias = self.root / kind
            if kind == "symlink":
                alias.symlink_to(self.path)
            else:
                os.link(self.path, alias)
            with self.assertRaises(CleanupError):
                write_private_json(alias, {"bad": True})
            self.assertEqual(self.path.read_bytes(), self.before)
            alias.unlink()

    def test_same_relative_absolute_parent_symlink_and_sidecar_paths_block(self):
        source = self.root / "state"
        source.write_bytes(b"protected")
        parent_alias = self.root / "alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        outputs = [source, self.root / ".." / self.root.name / "state", parent_alias / "state"]
        outputs += [Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal", ".lock")]
        outputs.append(Path(str(source) + ".recovery") / "1.json")
        for output in outputs:
            with self.subTest(output=output), self.assertRaises(CleanupError):
                PathSession(outputs=[output], state=source)
        self.assertEqual(source.read_bytes(), b"protected")

    def test_output_aliases_input_and_other_output(self):
        with self.assertRaises(CleanupError):
            PathSession(inputs=[self.path], outputs=[self.path])
        from argparse import Namespace
        from gmail_cleanup.paths import cli_paths
        args = Namespace(command="preflight", output=self.path, resolved_manifest=self.path)
        with self.assertRaises(CleanupError):
            cli_paths(args)

    def test_controlled_leaf_and_parent_replacement_fail_before_writing(self):
        target = self.root / "destination.json"
        session = PathSession(outputs=[target], inputs=[self.path])
        token = ACTIVE.set(session)
        try:
            os.link(self.path, target)
            with self.assertRaises(CleanupError):
                write_private_json(target, {"bad": True})
            self.assertEqual(self.path.read_bytes(), self.before)
        finally:
            ACTIVE.reset(token)
            session.close()

    def test_cli_alias_rejected_before_authentication_or_sqlite_initialization(self):
        state = self.root / "audit.sqlite"
        state.write_bytes(b"not even a database")
        manifest = self.root / "manifest.json"
        manifest.write_text('{}')
        with patch("gmail_cleanup.cli.backend") as backend, redirect_stdout(io.StringIO()):
            result = main(["preflight", "--env-auth", "--manifest", str(manifest), "--state", str(state), "--output", str(state)])
        self.assertEqual(result, 2)
        backend.assert_not_called()
        self.assertEqual(state.read_bytes(), b"not even a database")

    def test_offline_raw_source_alias_rejected_before_backend(self):
        inventory = self.root / "inventory.json"
        inventory.write_text(json.dumps({"fixture": {"raw_file": self.path.name}}))
        manifest = self.root / "manifest.json"
        manifest.write_text('{}')
        with patch("gmail_cleanup.cli.backend") as backend, redirect_stdout(io.StringIO()):
            result = main(["inspect", "--manifest", str(manifest), "--message-id", "fixture", "--offline-sources", str(inventory), "--output", str(self.path)])
        self.assertEqual(result, 2)
        backend.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_exclusive_concurrent_creators_never_clobber_winner(self):
        path = self.root / "exclusive.json"
        queue = multiprocessing.Queue()
        processes = [multiprocessing.Process(target=exclusive_creator, args=(path, i, queue)) for i in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(queue.get(timeout=2) for _ in processes), ["lost", "won"])
        self.assertIn(json.loads(path.read_text())["winner"], (0, 1))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_parent_replacement_between_validation_and_write_is_rejected(self):
        parent=self.root/'output';parent.mkdir(mode=0o700)
        protected=self.root/'protected';protected.mkdir(mode=0o700)
        source=protected/'report.json';source.write_bytes(b'protected-input')
        output=parent/'report.json'
        session=PathSession(inputs=[source],outputs=[output])
        token=ACTIVE.set(session)
        try:
            parent.rename(self.root/'original-output')
            parent.symlink_to(protected,target_is_directory=True)
            with self.assertRaises(CleanupError):write_private_json(output,{'bad':True})
            self.assertEqual(source.read_bytes(),b'protected-input')
            self.assertFalse((self.root/'original-output/report.json').exists())
        finally:
            ACTIVE.reset(token);session.close()

    def test_short_write_preserves_old_complete_artifact(self):
        original=os.fdopen
        class Short:
            def __init__(self,stream):self.stream=stream
            def __enter__(self):return self
            def __exit__(self,*args):self.stream.close()
            def fileno(self):return self.stream.fileno()
            def write(self,data):return self.stream.write(data[:3])
        with patch('gmail_cleanup.storage.os.fdopen',side_effect=lambda fd,mode:Short(original(fd,mode))):
            with self.assertRaises(OSError):write_private_json(self.path,{'new':True})
        self.assertEqual(self.path.read_bytes(),self.before)

    def test_auth_credential_output_alias_is_rejected_before_consent(self):
        with patch('gmail_cleanup.cli.authenticate') as auth,redirect_stdout(io.StringIO()):
            code=main(['auth','--credentials',str(self.path),'--token',str(self.path),'--modify','--drive'])
        self.assertEqual(code,2);auth.assert_not_called()
        self.assertEqual(self.path.read_bytes(),self.before)

    def test_sigkill_before_and_after_replace_never_publishes_partial_json(self):
        import signal
        context=multiprocessing.get_context('spawn')
        for phase in ('before','after'):
            write_private_json(self.path,{'old':True})
            parent,child=context.Pipe()
            process=context.Process(target=interrupted_replace,args=(str(self.path),phase,child))
            process.start()
            try:
                self.assertTrue(parent.poll(15));self.assertEqual(parent.recv(),phase)
                os.kill(process.pid,signal.SIGKILL);process.join(10)
                self.assertEqual(process.exitcode,-signal.SIGKILL)
                self.assertEqual(json.loads(self.path.read_bytes()),{'old':True} if phase=='before' else {'new':True})
            finally:
                if process.is_alive():process.kill();process.join(5)
                parent.close();child.close()

    def test_state_sidecars_cannot_alias_inputs_or_be_linked(self):
        state=self.root/'state.sqlite'
        for suffix in ('','-wal','-shm','-journal'):
            with self.subTest(suffix=suffix),self.assertRaises(CleanupError):
                PathSession(inputs=[Path(str(state)+suffix)],state=state)
        store=Store(state,initialize=True,mailbox_key='fake')
        store.close()
        for suffix in ('-wal','-shm','-journal'):
            for kind in ('symlink','hardlink'):
                sidecar=Path(str(state)+suffix)
                if kind=='symlink':sidecar.symlink_to(self.path)
                else:os.link(self.path,sidecar)
                with self.assertRaises(CleanupError):Store(state)
                self.assertEqual(self.path.read_bytes(),self.before)
                sidecar.unlink()

    def test_managed_state_links_and_insecure_journal_fail_before_authentication(self):
        manifest=self.root/'manifest.json';manifest.write_text('{}')
        for kind in ('symlink','hardlink','sidecar','journal'):
            with self.subTest(kind=kind):
                state=self.root/(kind+'.sqlite');journal=self.root/(kind+'.recovery')
                if kind=='symlink':state.symlink_to(self.path)
                if kind=='hardlink':os.link(self.path,state)
                if kind=='sidecar':Path(str(state)+'-wal').symlink_to(self.path)
                if kind=='journal':journal.symlink_to(self.root,target_is_directory=True)
                with patch('gmail_cleanup.cli.backend') as backend,redirect_stdout(io.StringIO()):
                    code=main(['preflight','--env-auth','--manifest',str(manifest),
                               '--state',str(state),'--journal',str(journal),'--output',str(self.root/(kind+'.json'))])
                self.assertEqual(code,2);backend.assert_not_called()
                self.assertEqual(self.path.read_bytes(),self.before)
                if kind=='hardlink':state.unlink()
