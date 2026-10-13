from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from gmail_cleanup.errors import CleanupError
from gmail_cleanup.fixtures import CASES, TEST_MARKER
from gmail_cleanup.manifest import load_manifest
from gmail_cleanup.mime import date_ms, parse
from .helpers import Store
from gmail_cleanup.synthetic import generate, register, run, prepare_backups
from .helpers import FakeGmail, FakeDrive


class SyntheticHarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "fixtures"
        generate(self.directory)
        self.index = json.loads((self.directory / "fixtures.json").read_text())
        messages = {}
        self.ids = {}
        for number, case in enumerate(CASES):
            message_id = f"abc{number}"
            self.ids[case] = message_id
            raw = (self.directory / self.index["cases"][case]["raw_file"]).read_bytes()
            messages[message_id] = {"id": message_id, "raw_bytes": raw,
                                    "threadId": "threadAB" if case.startswith("thread-") else f"thread{number}",
                                    "internalDate": str(date_ms(parse(raw))),
                                    "labelIds": ["SENT", "STARRED"] if case == "sent" else ["INBOX", "UNREAD", "STARRED"]}
        self.api = FakeGmail(messages)
        self.manifest_path = self.root / "synthetic.json"
        self.store = Store(self.root / "state.sqlite")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def registered(self):
        register(self.api, self.directory, self.ids, self.manifest_path)
        return load_manifest(self.manifest_path)

    def test_registration_and_dry_run_only_read_explicit_synthetic_ids(self):
        manifest = self.registered()
        result = run(self.api, self.store, manifest)
        self.assertEqual(len(result["results"]), len(CASES))
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.api.inserts, [])
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(set(self.api.reads), set(self.ids.values()))

    def test_full_harness_preserves_originals_sent_state_threads_and_retries(self):
        before = deepcopy(self.api.messages)
        manifest = self.registered()
        result = run(self.api, self.store, manifest, execute=True)
        self.assertEqual(len(self.api.inserts), len(CASES))
        self.assertTrue(all(r["state"] == "verified" and r["original_unchanged"] for r in result["results"]))
        for message_id, original in before.items():
            self.assertEqual(self.api.messages[message_id], original)
        thread_cases = [r for r in result["results"] if r["case"].startswith("thread-")]
        self.assertEqual({self.api.messages[r["replacement_id"]]["threadId"] for r in thread_cases}, {"threadAB"})
        run(self.api, self.store, manifest, execute=True)
        self.assertEqual(len(self.api.inserts), len(CASES))
        self.assertEqual(self.api.trashes, [])

    def test_registered_backup_preflight_preserves_registry_and_originals(self):
        manifest = self.registered()
        self.api.drive = FakeDrive()
        before = deepcopy(self.api.messages)
        report, resolved = prepare_backups(self.api, self.store, manifest, allow_upload=True)
        self.assertTrue(report["all_ready_to_create"])
        self.assertEqual(resolved["synthetic"], manifest["synthetic"])
        self.assertEqual(self.api.messages, before)
        self.assertEqual(self.api.inserts + self.api.trashes, [])
        result = run(self.api, self.store, resolved, execute=True)
        self.assertTrue(all(r["state"] == "verified" for r in result["results"]))

    def test_changed_registered_fixture_blocks_all_drive_uploads(self):
        manifest = self.registered()
        self.api.drive = FakeDrive()
        self.api.messages[self.ids["thread-b"]]["raw_bytes"] += b"changed"
        with self.assertRaises(CleanupError):
            prepare_backups(self.api, self.store, manifest, allow_upload=True)
        self.assertEqual(self.api.drive.creates, [])
        self.assertEqual(self.api.inserts + self.api.trashes, [])

    def test_unmarked_real_style_message_fails_registration_without_writes(self):
        original = self.api.messages[self.ids["plain"]]
        msg = parse(original["raw_bytes"])
        del msg[TEST_MARKER]
        original["raw_bytes"] = msg.as_bytes()
        with self.assertRaises(CleanupError):
            self.registered()
        self.assertFalse(self.manifest_path.exists())
        self.assertEqual(self.api.inserts, [])

    def test_changed_original_blocks_entire_run_before_any_insert(self):
        manifest = self.registered()
        self.api.messages[self.ids["thread-b"]]["raw_bytes"] += b"tampered"
        with self.assertRaises(CleanupError):
            run(self.api, self.store, manifest, execute=True)
        self.assertEqual(self.api.inserts, [])

    def test_sent_case_cannot_claim_sent_preservation_without_sent_original(self):
        self.api.messages[self.ids["sent"]]["labelIds"].remove("SENT")
        with self.assertRaises(CleanupError):
            self.registered()
        self.assertEqual(self.api.inserts, [])

    def test_thread_cases_require_a_real_shared_original_thread(self):
        self.api.messages[self.ids["thread-b"]]["threadId"] = "separate-thread"
        with self.assertRaises(CleanupError):
            self.registered()
        self.assertEqual(self.api.inserts, [])

    def test_general_cleanup_manifest_cannot_run_as_synthetic(self):
        manifest = self.registered()
        del manifest["synthetic"]
        with self.assertRaises(CleanupError):
            run(self.api, self.store, manifest, execute=True)
        self.assertEqual(self.api.inserts, [])

    def test_response_loss_is_detected_without_touching_original_or_inserting_again(self):
        manifest = self.registered()
        self.api.lose_insert_response = True
        with self.assertRaises(CleanupError):
            run(self.api, self.store, manifest, execute=True)
        self.assertEqual(len(self.api.inserts), 1)
        with self.assertRaises(CleanupError):
            run(self.api, self.store, manifest, execute=True)
        self.assertEqual(len(self.api.inserts), 1)
        self.assertEqual(self.api.trashes, [])
