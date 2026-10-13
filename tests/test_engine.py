from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from gmail_cleanup.engine import (check_replacement, create, plan_message, reconcile,
                                 require_record, trash_original)
from gmail_cleanup.errors import CleanupError, Unsupported
from gmail_cleanup.mime import parse
from .helpers import Store
from .helpers import FakeGmail, message


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "audit.sqlite"
        self.store = Store(self.path)
        self.original, self.manifest = message()
        self.api = FakeGmail({"abc123": self.original})

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def plan(self):
        return plan_message(self.api, self.store, self.manifest, "abc123")

    def create(self):
        self.plan()
        return create(self.api, self.store, self.manifest, "abc123", execute=True)

    def approve_trash(self, record, **overrides):
        kwargs = {"execute": True, "enable_trash": True,
                  "approve_plan": record["plan"]["plan_sha256"],
                  "approve_replacement": record["replacement_id"]}
        kwargs.update(overrides)
        return trash_original(self.api, self.store, self.manifest, "abc123", **kwargs)

    def test_plan_is_read_only_exact_and_repeatable(self):
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertEqual(self.api.inserts, [])
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(first["plan"]["removed"][0]["part"], "0.1")
        self.assertEqual(first["plan"]["insert"]["method"], "users.messages.insert")
        self.assertEqual(first["plan"]["expected"]["date_header_ms"], int(self.original["internalDate"]))

    def test_default_create_and_trash_are_dry_runs_without_even_a_mailbox_read(self):
        self.plan()
        self.api.reads.clear()
        self.assertEqual(create(None, self.store, self.manifest, "abc123")["state"], "planned")
        self.assertEqual(trash_original(None, self.store, self.manifest, "abc123")["state"], "planned")
        self.assertEqual(self.api.reads, [])
        self.assertEqual(self.api.inserts, [])
        self.assertEqual(self.api.trashes, [])

    def test_create_verifies_and_preserves_original_duplicate_message_id(self):
        record = self.create()
        self.assertEqual(record["state"], "verified")
        self.assertTrue(record["verification"]["passed"])
        self.assertEqual(self.api.messages["abc123"], self.original)
        old = parse(self.original["raw_bytes"])
        new = parse(self.api.messages[record["replacement_id"]]["raw_bytes"])
        self.assertEqual(old["Message-ID"], new["Message-ID"])
        self.assertEqual(self.api.trashes, [])

    def test_create_retry_and_restart_never_insert_twice(self):
        first = self.create()
        self.store.close()
        self.store = Store(self.path)
        second = create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(first["replacement_id"], second["replacement_id"])
        self.assertEqual(len(self.api.inserts), 1)

    def test_lost_insert_response_blocks_retry_and_can_be_reconciled(self):
        self.plan()
        self.api.lose_insert_response = True
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(self.store.get("abc123")["state"], "insert_pending")
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(len(self.api.inserts), 1)
        self.assertTrue(reconcile(self.api, self.store, self.manifest, "abc123", "replacement1")["passed"])
        self.assertIsNone(self.store.get("abc123")["replacement_id"])
        recovered = reconcile(self.api, self.store, self.manifest, "abc123", "replacement1", execute=True)
        self.assertEqual(recovered["state"], "verified")
        self.assertEqual(self.api.messages["abc123"], self.original)

    def test_rejected_insert_is_conservatively_uncertain_and_never_retried(self):
        self.plan()
        self.api.reject_insert = True
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(self.store.get("abc123")["state"], "insert_pending")
        self.assertEqual(self.api.messages["abc123"], self.original)

    def test_crash_after_insert_before_id_persistence_is_recoverable(self):
        self.plan()
        original_save = self.store.save
        def crash(record, **kwargs):
            if record["state"] == "inserted":
                raise RuntimeError("simulated process death")
            return original_save(record, **kwargs)
        self.store.save = crash
        with self.assertRaises(RuntimeError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.store.save = original_save
        self.assertEqual(self.store.get("abc123")["state"], "insert_pending")
        self.assertEqual(len(self.api.inserts), 1)
        self.assertEqual(reconcile(self.api, self.store, self.manifest, "abc123", "replacement1", execute=True)["state"], "verified")

    def test_reconcile_refuses_original_and_unrelated_replacement(self):
        self.plan()
        self.api.lose_insert_response = True
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        for candidate in ("abc123",):
            with self.assertRaises(CleanupError):
                reconcile(self.api, self.store, self.manifest, "abc123", candidate, execute=True)
        raw = parse(self.api.messages["replacement1"]["raw_bytes"])
        raw.replace_header("X-Gmail-Attachment-Cleanup", "unrelated-operation")
        self.api.messages["replacement1"]["raw_bytes"] = raw.as_bytes()
        with self.assertRaises(CleanupError):
            reconcile(self.api, self.store, self.manifest, "abc123", "replacement1", execute=True)
        self.assertIsNone(self.store.get("abc123")["replacement_id"])

    def test_missing_attachment_label_thread_header_timestamp_each_block_verification(self):
        for field in ("labels", "thread", "body", "header", "timestamp", "classification"):
            with self.subTest(field=field):
                directory = tempfile.TemporaryDirectory()
                store = Store(Path(directory.name) / "state.sqlite")
                api = FakeGmail({"abc123": self.original})
                def corrupt(item):
                    if field == "labels": item["labelIds"].remove("STARRED")
                    elif field == "thread": item["threadId"] = "different"
                    elif field == "timestamp": item["internalDate"] = "0"
                    elif field == "classification": item["classificationLabelValues"] = [{"labelId": "unexpected"}]
                    else:
                        msg = parse(item["raw_bytes"])
                        if field == "body": msg.get_payload()[0].set_content("corrupted body")
                        else: msg.replace_header("Subject", "changed")
                        item["raw_bytes"] = msg.as_bytes()
                api.corrupt = corrupt
                plan_message(api, store, self.manifest, "abc123")
                record = create(api, store, self.manifest, "abc123", execute=True)
                self.assertEqual(record["state"], "verify_failed")
                self.assertEqual(record["replacement_id"], "replacement1")
                self.assertEqual(api.messages["abc123"], self.original)
                with self.assertRaises(CleanupError):
                    trash_original(api, store, self.manifest, "abc123", execute=True, enable_trash=True,
                                   approve_plan=record["plan"]["plan_sha256"], approve_replacement="replacement1")
                self.assertEqual(api.trashes, [])
                store.close()
                directory.cleanup()

    def test_date_header_and_received_timestamp_difference_is_reported_and_blocks_trash(self):
        self.api.messages["abc123"]["internalDate"] = str(int(self.original["internalDate"]) + 123456)
        record = self.create()
        self.assertEqual(record["plan"]["ordering_delta_ms_if_date_header_used"], -123456)
        self.assertEqual(record["verification"]["differences"]["ordering_delta_ms"], -123456)
        self.assertEqual(record["state"], "verify_failed")
        self.assertFalse(record["verification"]["checks"]["original_ordering_timestamp"])

    def test_sent_state_is_requested_and_loss_is_not_silently_accepted(self):
        original, manifest = message("sent")
        self.api = FakeGmail({"abc123": original})
        self.manifest = manifest
        record = self.create()
        self.assertIn("SENT", record["plan"]["insert"]["labels"])
        self.assertEqual(record["state"], "verified")
        replacement = self.api.messages[record["replacement_id"]]
        replacement["labelIds"].remove("SENT")
        result = check_replacement(replacement, record["plan"])
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["labels_read_unread_starred_sent"])

    def test_failed_replacement_fetch_keeps_id_and_original(self):
        self.plan()
        self.api.fail_get.add("replacement1")
        record = create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(record["state"], "verify_failed")
        self.assertEqual(record["replacement_id"], "replacement1")
        self.assertEqual(self.api.messages["abc123"], self.original)

    def test_message_thread_id_alone_is_not_enough_for_verification(self):
        self.api.get_thread = lambda thread: {"id": thread, "messages": [{"id": "abc123"}]}
        record = self.create()
        self.assertEqual(record["state"], "verify_failed")
        self.assertFalse(record["verification"]["checks"]["thread_contains_original_and_replacement"])
        self.assertEqual(self.api.trashes, [])

    def test_missing_unselected_attachment_blocks_verification(self):
        original, manifest = message("multiple")
        self.api = FakeGmail({"abc123": original})
        self.manifest = manifest
        def drop_unselected(item):
            msg = parse(item["raw_bytes"])
            msg.set_payload(msg.get_payload()[:-1])
            item["raw_bytes"] = msg.as_bytes()
        self.api.corrupt = drop_unselected
        record = self.create()
        self.assertEqual(record["state"], "verify_failed")
        self.assertFalse(record["verification"]["checks"]["mime_contents_and_attachment_hashes"])
        self.assertEqual(self.api.messages["abc123"], original)

    def test_changed_original_or_manifest_cannot_be_used_for_creation(self):
        self.plan()
        self.api.messages["abc123"]["labelIds"].remove("UNREAD")
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.api.messages["abc123"] = deepcopy(self.original)
        self.manifest["messages"]["abc123"][0]["drive_url"] += "?changed=1"
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(self.api.inserts, [])

    def test_allowlist_is_enforced_before_any_fetch(self):
        with self.assertRaises(CleanupError):
            plan_message(self.api, self.store, self.manifest, "notallowed")
        self.assertEqual(self.api.reads, [])

    def test_trash_needs_each_separate_gate_and_successful_verification(self):
        record = self.create()
        for overrides in ({"enable_trash": False}, {"approve_plan": "wrong"}, {"approve_replacement": "wrong"}):
            with self.subTest(overrides=overrides), self.assertRaises(CleanupError):
                self.approve_trash(record, **overrides)
        self.assertEqual(self.api.trashes, [])
        result = self.approve_trash(record)
        self.assertEqual(result["state"], "trashed")
        self.assertEqual(self.api.trashes, ["abc123"])
        self.approve_trash(record)
        self.assertEqual(self.api.trashes, ["abc123"])

    def test_fresh_verification_blocks_trash_if_copy_changed_later(self):
        record = self.create()
        self.api.messages[record["replacement_id"]]["threadId"] = "wrong"
        with self.assertRaises(CleanupError):
            self.approve_trash(record)
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(self.api.messages["abc123"], self.original)

    def test_interrupted_trash_resolves_by_read_only_inspection(self):
        record = self.create()
        self.api.lose_trash_response = True
        with self.assertRaises(CleanupError):
            self.approve_trash(record)
        self.assertEqual(self.store.get("abc123")["state"], "trash_pending")
        self.assertEqual(self.approve_trash(record)["state"], "trashed")
        self.assertEqual(self.api.trashes, ["abc123"])

    def test_private_state_contains_no_body_subject_or_address(self):
        self.create()
        for _, text in self.store.db.execute("SELECT message_id,record FROM events"):
            self.assertNotIn("Synthetic correspondence", text)
            self.assertNotIn("sender@example.invalid", text)
            self.assertNotIn("GMAIL-CLEANUP-SYNTHETIC", text)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_atomic_state_transition_refuses_stale_writer(self):
        record = self.plan()
        record["state"] = "insert_pending"
        self.store.save(record, expected_state="planned")
        with self.assertRaises(CleanupError):
            self.store.save(record, expected_state="planned")

    def test_unsupported_metadata_is_skipped(self):
        for label in ("TRASH", "DRAFT", "DRAFTS", "SPAM"):
            self.api.messages["abc123"] = deepcopy(self.original)
            self.api.messages["abc123"]["labelIds"].append(label)
            with self.subTest(label=label), self.assertRaises(Unsupported):
                self.plan()
        self.assertEqual(self.api.inserts, [])
