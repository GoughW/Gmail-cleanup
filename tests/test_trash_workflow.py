from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from gmail_cleanup.api import DRIVE_SCOPE, MODIFY_SCOPE, Gmail, authenticate
from gmail_cleanup.cli import main
from gmail_cleanup.engine import (create, plan_message, trash_original,
                                 verify_after_trash)
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.mime import parse, parts, digest
from gmail_cleanup.storage import Store, write_private_json
from gmail_cleanup.workflow import run
from .helpers import FakeDrive, FakeGmail, message


class TrashAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")
        self.original, self.manifest = message()
        self.api = FakeGmail({"abc123": self.original})
        plan_message(self.api, self.store, self.manifest, "abc123")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self):
        return create(self.api, self.store, self.manifest, "abc123", execute=True)

    def remove(self, record, **changes):
        kwargs = {"execute": True, "enable_trash": True,
                  "approve_plan": record["plan"]["plan_sha256"],
                  "approve_replacement": record["replacement_id"]}
        kwargs.update(changes)
        return trash_original(self.api, self.store, self.manifest, "abc123", **kwargs)

    def test_dry_run_makes_no_api_request(self):
        self.api.events.clear()
        record = trash_original(None, self.store, self.manifest, "abc123")
        self.assertEqual(record["state"], "planned")
        self.assertEqual(self.api.events, [])

    def test_planned_original_can_never_be_removed(self):
        record = self.store.get("abc123")
        with self.assertRaises(CleanupError):
            self.remove(record, approve_replacement="fabricated")
        self.assertEqual(self.api.trashes, [])

    def test_all_enablement_and_exact_approval_gates_are_required(self):
        record = self.create()
        for change in ({"enable_trash": False}, {"approve_plan": "wrong"},
                       {"approve_replacement": "wrong"}, {"approve_replacement": None}):
            with self.subTest(change=change), self.assertRaises(CleanupError):
                self.remove(record, **change)
        self.assertEqual(self.api.trashes, [])

    def test_modify_capability_is_required_even_after_verified_creation(self):
        record = self.create()
        self.api.can_modify = False
        with self.assertRaises(CleanupError):
            self.remove(record)
        self.assertEqual(self.api.trashes, [])

    def test_success_records_snapshots_and_confirms_original_retained_in_trash(self):
        record = self.remove(self.create())
        self.assertEqual(record["state"], "trashed")
        self.assertTrue(record["post_trash_verification"]["passed"])
        self.assertIn("TRASH", self.api.messages["abc123"]["labelIds"])
        self.assertIn(record["replacement_id"], self.api.messages)
        self.assertEqual(record["trash_audit"]["original"]["id"], "abc123")
        self.assertEqual(record["trash_audit"]["original"]["headers"],
                         record["trash_audit"]["replacement_after_trash"]["headers"])
        self.assertEqual(self.api.trashes, ["abc123"])
        self.assertIn("abc123", self.api.messages)

    def test_corrupted_replacement_before_trash_keeps_original(self):
        record = self.create()
        self.api.messages[record["replacement_id"]]["labelIds"].remove("UNREAD")
        with self.assertRaises(CleanupError):
            self.remove(record)
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(self.api.messages["abc123"], self.original)
        self.assertEqual(self.store.get("abc123")["state"], "verify_failed")

    def test_changed_drive_before_trash_keeps_original(self):
        record = self.create()
        self.api.drive.corrupt_download = True
        with self.assertRaises(CleanupError):
            self.remove(record)
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(self.api.messages["abc123"], self.original)

    def test_trash_intent_and_requested_audit_precede_the_api_call(self):
        trash = self.api.trash
        def check_intent(key):
            saved = self.store.get(key)
            self.assertEqual(saved["state"], "trash_pending")
            self.assertEqual(saved["approval"]["action"], "trash-original")
            self.assertEqual(saved["trash_audit"]["original"]["id"], key)
            return trash(key)
        self.api.trash = check_intent
        self.remove(self.create())

    def test_lost_trash_response_is_recovered_read_only(self):
        record = self.create()
        self.api.lose_trash_response = True
        with self.assertRaises(CleanupError):
            self.remove(record)
        self.assertEqual(self.store.get("abc123")["state"], "trash_pending")
        self.assertIn("TRASH", self.api.messages["abc123"]["labelIds"])
        self.store.close()
        self.store = Store(self.root / "state.sqlite")
        recovered = self.remove(self.store.get("abc123"))
        self.assertEqual(recovered["state"], "trashed")
        self.assertEqual(self.api.trashes, ["abc123"])

    def test_rejected_trash_stays_pending_without_blind_retry(self):
        record = self.create()
        self.api.reject_trash = True
        with self.assertRaises(CleanupError):
            self.remove(record)
        self.api.reject_trash = False
        with self.assertRaises(CleanupError):
            self.remove(self.store.get("abc123"))
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(self.api.messages["abc123"], self.original)

    def test_original_fetch_failure_cannot_confirm_trash(self):
        record = self.create()
        trash = self.api.trash
        def remove_then_fail(key):
            trash(key)
            self.api.fail_get.add(key)
        self.api.trash = remove_then_fail
        with self.assertRaises(CleanupError):
            self.remove(record)
        self.assertEqual(self.store.get("abc123")["state"], "trash_pending")

    def test_post_trash_change_is_reported_with_original_retained(self):
        record = self.create()
        trash = self.api.trash
        def remove_then_change(key):
            trash(key)
            self.api.messages[record["replacement_id"]]["threadId"] = "wrong-thread"
        self.api.trash = remove_then_change
        result = self.remove(record)
        self.assertEqual(result["state"], "trashed_verify_failed")
        self.assertFalse(result["post_trash_verification"]["passed"])
        self.assertIn("TRASH", self.api.messages["abc123"]["labelIds"])

    def test_create_never_reinserts_after_original_trash(self):
        self.remove(self.create())
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(len(self.api.inserts), 1)


class SequentialWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")
        self.messages, choices = {}, {}
        for index in range(5):
            key = f"abc{index}"
            item, selection = message("thread-b" if index else "thread-a", message_id=key)
            self.messages[key] = item
            choices[key] = [{"part": c["part"], "decoded_sha256": c["decoded_sha256"]}
                            for c in selection["messages"][key]]
        self.manifest = {"version": 2, "allowlist": list(self.messages), "messages": choices}
        self.api = FakeGmail(self.messages)
        self.api.drive = FakeDrive()
        self.output = self.root / "report.json"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_workflow(self, **changes):
        kwargs = {"execute": True, "enable_trash": True,
                  "disposable_test_account": True, "allow_upload": True}
        kwargs.update(changes)
        return run(self.api, self.store, self.manifest, self.output, **kwargs)

    def test_five_messages_complete_in_order_with_hash_and_metadata_audit(self):
        report = self.run_workflow()
        self.assertTrue(report["completed"])
        self.assertEqual(self.api.trashes, self.manifest["allowlist"])
        self.assertEqual(len(self.api.inserts), 5)
        self.assertEqual(len(self.api.drive.creates), 2)  # one folder and shared file
        self.assertEqual(len(self.api.messages), 10)  # Originals retained in Trash.
        for index, row in enumerate(report["results"]):
            self.assertEqual(row["state"], "trashed")
            self.assertTrue(row["verification"]["passed"])
            self.assertTrue(row["post_trash_verification"]["passed"])
            self.assertTrue(all(row["header_preservation"].values()))
            self.assertEqual(row["original"]["thread_id"], row["replacement"]["thread_id"])
            self.assertEqual(row["original"]["labels"], row["replacement"]["labels"])
            self.assertEqual(row["original"]["internal_date_ms"], row["replacement"]["internal_date_ms"])
            proof = row["drive_backups"][0]
            self.assertEqual(proof["source_sha256"], proof["downloaded_sha256"])
            self.assertIn("Gmail attachment backups/", proof["path"])
            if index < 4:
                trashed_at = self.api.events.index(("trash", row["original_id"]))
                next_start = self.api.events.index(("get", report["results"][index + 1]["original_id"]))
                self.assertLess(trashed_at, next_start)
        self.assertEqual(json.loads(self.output.read_text()), report)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_first_failed_replacement_stops_before_trashing_or_reading_next_original(self):
        def corrupt(item):
            item["internalDate"] = str(int(item["internalDate"]) + 1)
        self.api.corrupt = corrupt
        report = self.run_workflow()
        self.assertFalse(report["completed"])
        self.assertEqual(report["stopped_at"], "abc0")
        self.assertEqual(report["results"][0]["state"], "verify_failed")
        self.assertEqual(report["results"][0]["replacement_id"], "replacement1")
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(self.api.messages["abc0"], self.messages["abc0"])
        self.assertEqual(set(self.api.reads), {"abc0", "replacement1"})
        self.assertTrue(all(row["state"] == "not_processed" for row in report["results"][1:]))

    def test_second_failure_keeps_second_original_and_stops_remaining_three(self):
        def corrupt_second(item):
            if len(self.api.inserts) == 1:
                item["labelIds"].remove("UNREAD")
        self.api.corrupt = corrupt_second
        report = self.run_workflow()
        self.assertEqual(report["stopped_at"], "abc1")
        self.assertEqual(self.api.trashes, ["abc0"])
        self.assertEqual(len(self.api.inserts), 2)
        self.assertEqual(self.api.messages["abc1"], self.messages["abc1"])
        self.assertNotIn("abc2", self.api.reads)

    def test_resume_completed_run_makes_no_second_insert_or_trash(self):
        self.run_workflow()
        self.store.close()
        self.store = Store(self.root / "state.sqlite")
        again = self.run_workflow()
        self.assertTrue(again["completed"])
        self.assertEqual(len(self.api.inserts), 5)
        self.assertEqual(self.api.trashes, self.manifest["allowlist"])
        self.assertTrue(all(row["replacement"] for row in again["results"]))

    def test_drive_failure_stops_before_any_insert(self):
        self.api.drive.corrupt_download = True
        report = self.run_workflow()
        self.assertEqual(report["stopped_at"], "abc0")
        self.assertEqual(self.api.inserts + self.api.trashes, [])
        self.assertNotIn("abc1", self.api.reads)

    def test_hash_failure_on_first_attachment_does_not_upload_second_attachment(self):
        source, _ = message("multiple", message_id="abc0")
        self.api = FakeGmail({"abc0": source})
        self.api.drive = FakeDrive()
        self.api.drive.corrupt_download = True
        choices = [{"part": path, "decoded_sha256": digest(p.get_payload(decode=True))}
                   for path, p in parts(parse(source["raw_bytes"])) if p.get_content_disposition() == "attachment"]
        self.manifest = {"version": 2, "allowlist": ["abc0"], "messages": {"abc0": choices}}
        report = self.run_workflow()
        self.assertFalse(report["completed"])
        self.assertEqual(len(report["results"][0]["drive_backups"]), 1)
        self.assertEqual(len(self.api.drive.creates), 2)  # folder and first attachment only
        self.assertEqual(self.api.inserts + self.api.trashes, [])

    def test_metadata_change_during_preparation_blocks_insertion(self):
        get = self.api.get
        reads = 0
        def change_during_prep(key):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.api.messages[key]["labelIds"].remove("UNREAD")
            return get(key)
        self.api.get = change_during_prep
        report = self.run_workflow()
        self.assertFalse(report["completed"])
        self.assertEqual(report["stopped_at"], "abc0")
        self.assertIn("changed during workflow", report["error"])
        self.assertEqual(self.api.inserts + self.api.trashes, [])

    def test_missing_capability_or_flags_make_no_drive_or_mail_mutation(self):
        for change in ({"enable_trash": False}, {"disposable_test_account": False}):
            report = self.run_workflow(**change)
            self.assertEqual(report["results"][0]["state"], "blocked_setup")
        self.api.can_modify = False
        report = self.run_workflow()
        self.assertEqual(report["results"][0]["state"], "blocked_setup")
        self.assertEqual(self.api.inserts + self.api.trashes + self.api.drive.creates, [])

    def test_default_dry_run_cannot_upload_insert_or_trash(self):
        report = run(self.api, self.store, self.manifest, self.output)
        self.assertFalse(report["completed"])  # no existing retained copy
        self.assertEqual(self.api.drive.creates + self.api.inserts + self.api.trashes, [])

    def test_lost_insert_response_stops_and_never_retries_on_next_run(self):
        self.api.lose_insert_response = True
        report = self.run_workflow()
        self.assertEqual(report["results"][0]["state"], "insert_pending")
        self.assertEqual(self.api.trashes, [])
        self.run_workflow()
        self.assertEqual(len(self.api.inserts), 1)
        self.assertNotIn("abc1", self.api.reads)

    def test_changed_manifest_cannot_resume_an_existing_trash(self):
        self.run_workflow()
        self.manifest["messages"]["abc0"][0]["decoded_sha256"] = "0" * 64
        report = self.run_workflow()
        self.assertFalse(report["completed"])
        self.assertEqual(report["stopped_at"], "abc0")
        self.assertEqual(len(self.api.inserts), 5)
        self.assertEqual(len(self.api.trashes), 5)

    def test_lost_trash_response_stops_then_recovers_without_second_write(self):
        self.api.lose_trash_response = True
        first = self.run_workflow()
        self.assertEqual(first["stopped_at"], "abc0")
        self.assertEqual(self.store.get("abc0")["state"], "trash_pending")
        self.assertEqual(self.api.trashes, ["abc0"])
        self.assertNotIn("abc1", self.api.reads)
        self.api.lose_trash_response = False
        recovered = self.run_workflow()
        self.assertTrue(recovered["completed"])
        self.assertEqual(self.api.trashes, self.manifest["allowlist"])
        self.assertEqual(len(self.api.inserts), 5)

    def test_post_trash_replacement_failure_stops_before_next_message(self):
        trash = self.api.trash
        def corrupt_after_trash(key):
            result = trash(key)
            self.api.messages["replacement1"]["labelIds"].append("TRASH")
            return result
        self.api.trash = corrupt_after_trash
        report = self.run_workflow()
        self.assertFalse(report["completed"])
        self.assertEqual(report["stopped_at"], "abc0")
        row = report["results"][0]
        self.assertEqual(row["state"], "trashed_verify_failed")
        self.assertTrue(row["original_trashed"])
        self.assertFalse(row["post_trash_verification"]["passed"])
        self.assertNotIn("abc1", self.api.reads)
        self.assertIn("abc0", self.api.messages)

    def test_post_trash_drive_failure_stops_and_is_reported_specifically(self):
        trash = self.api.trash
        def corrupt_drive_after_trash(key):
            result = trash(key)
            self.api.drive.corrupt_download = True
            return result
        self.api.trash = corrupt_drive_after_trash
        report = self.run_workflow()
        row = report["results"][0]
        self.assertFalse(row["post_trash_verification"]["checks"]["retained_drive_files"])
        self.assertEqual(row["state"], "trashed_verify_failed")
        self.assertNotIn("abc1", self.api.reads)

    def test_trash_label_changes_are_audited_and_replacement_labels_stay_exact(self):
        trash = self.api.trash
        def remove_inbox_after_trash(key):
            result = trash(key)
            self.api.messages[key]["labelIds"].remove("INBOX")
            return result
        self.api.trash = remove_inbox_after_trash
        report = self.run_workflow()
        self.assertTrue(report["completed"])
        for row in report["results"]:
            self.assertIn("TRASH", row["original_after_trash"]["labels"])
            self.assertNotIn("INBOX", row["original_after_trash"]["labels"])
            self.assertEqual(row["original"]["labels"], row["replacement"]["labels"])
            self.assertEqual(row["original"]["headers"], row["original_after_trash"]["headers"])

    def test_post_trash_thread_view_may_omit_original_but_must_include_copy(self):
        get_thread = self.api.get_thread
        def filter_trash(thread_id):
            result = get_thread(thread_id)
            result["messages"] = [m for m in result["messages"]
                                  if "TRASH" not in self.api.messages[m["id"]]["labelIds"]]
            return result
        self.api.get_thread = filter_trash
        report = self.run_workflow()
        self.assertTrue(report["completed"])
        self.assertTrue(all(row["post_trash_verification"]["checks"]["thread_contains_replacement"]
                            for row in report["results"]))

    def test_cli_modify_drive_workflow_succeeds_and_reports_trash_count(self):
        path = self.root / "manifest.json"
        write_private_json(path, self.manifest)
        stdout = io.StringIO()
        with patch("gmail_cleanup.cli.Gmail.from_environment", return_value=self.api) as factory, redirect_stdout(stdout):
            code = main(["workflow", "--env-auth", "--manifest", str(path),
                         "--state", str(self.root / "cli.sqlite"), "--output", str(self.output),
                         "--execute", "--enable-trash", "--disposable-test-account", "--upload-backups"])
        self.assertEqual(code, 0)
        factory.assert_called_once_with(require_modify=True)
        self.assertEqual(json.loads(stdout.getvalue())["trashed_original_count"], 5)
        self.assertEqual(self.api.trashes, self.manifest["allowlist"])

    def test_supported_layouts_complete_full_workflow_and_retain_originals(self):
        for case in ("plain", "html", "received", "sent", "multiple", "inline", "unicode", "duplicate"):
            with self.subTest(case=case):
                key = "case-" + case
                item, manifest = message(case, message_id=key)
                api = FakeGmail({key: item})
                api.drive = FakeDrive()
                selections = [{"part": p["part"], "decoded_sha256": p["decoded_sha256"]}
                              for p in manifest["messages"][key]]
                batch = {"version": 2, "allowlist": [key], "messages": {key: selections}}
                store = Store(self.root / (case + ".sqlite"))
                try:
                    result = run(api, store, batch, self.output, execute=True, enable_trash=True,
                                 disposable_test_account=True, allow_upload=True)
                finally:
                    store.close()
                self.assertTrue(result["completed"])
                self.assertIn(key, api.messages)
                self.assertIn("TRASH", api.messages[key]["labelIds"])
                self.assertEqual(result["results"][0]["original"]["labels"],
                                 result["results"][0]["replacement"]["labels"])

    def test_cli_requires_modify_scope_and_saves_blocked_report_without_mutations(self):
        path = self.root / "manifest.json"
        write_private_json(path, self.manifest)
        stdout = io.StringIO()
        with patch("gmail_cleanup.cli.Gmail.from_environment", side_effect=CleanupError("Modify grant missing")) as factory, redirect_stdout(stdout):
            code = main(["workflow", "--env-auth", "--manifest", str(path), "--state", str(self.root / "cli.sqlite"),
                         "--output", str(self.output), "--execute", "--enable-trash", "--disposable-test-account", "--upload-backups"])
        factory.assert_called_once_with(require_modify=True)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(self.output.read_text())["results"][0]["state"], "blocked_setup")
        self.assertNotIn("Synthetic correspondence", stdout.getvalue())


class TrashApiTests(unittest.TestCase):
    def setUp(self):
        self.api = Gmail.__new__(Gmail)
        self.api.service = MagicMock()
        self.api.can_modify = True
        self.api.credentials = MagicMock(granted_scopes=[MODIFY_SCOPE, DRIVE_SCOPE])
        self.messages = self.api.service.users.return_value.messages.return_value

    def test_only_exact_trash_endpoint_and_no_automatic_retry(self):
        self.messages.trash.return_value.execute.return_value = {"id": "explicit-original"}
        self.api.trash("explicit-original")
        self.messages.trash.assert_called_once_with(userId="me", id="explicit-original")
        self.messages.trash.return_value.execute.assert_called_once_with(num_retries=0)
        self.messages.batchDelete.assert_not_called()
        self.messages.delete.assert_not_called()
        self.messages.send.assert_not_called()
        self.api.service.users.return_value.threads.return_value.trash.assert_not_called()
        self.api.service.users.return_value.threads.return_value.delete.assert_not_called()
        self.assertFalse(hasattr(Gmail, "delete"))

    def test_explicit_modify_capability_is_required(self):
        self.api.can_modify = False
        with self.assertRaises(CleanupError):
            self.api.trash("original")
        self.messages.trash.assert_not_called()

    def test_http404_and_auth_or_transport_errors_cannot_confirm_trash(self):
        request = self.messages.get.return_value
        exc = RuntimeError("private API response")
        exc.resp = types.SimpleNamespace(status=404)
        request.execute.side_effect = exc
        for status in (404, 401, 403, 500, None):
            exc.resp.status = status
            with self.subTest(status=status), self.assertRaises(CleanupError):
                self.api.get("original")

    def test_desktop_workflow_requests_only_modify_and_drive_file(self):
        module = types.ModuleType("google_auth_oauthlib.flow")
        module.InstalledAppFlow = MagicMock()
        flow = module.InstalledAppFlow.from_client_secrets_file.return_value
        flow.run_local_server.return_value.refresh_token = "synthetic-secret"
        flow.run_local_server.return_value.to_json.return_value = json.dumps({"scopes": [MODIFY_SCOPE, DRIVE_SCOPE]})
        with tempfile.TemporaryDirectory() as directory, patch.dict("sys.modules", {"google_auth_oauthlib.flow": module}):
            authenticate("dummy.json", Path(directory) / "token.json", modify=True, drive=True)
        module.InstalledAppFlow.from_client_secrets_file.assert_called_once_with("dummy.json", [MODIFY_SCOPE, DRIVE_SCOPE])

    def test_unknown_commands_and_flags_are_rejected(self):
        from gmail_cleanup.cli import parser
        from contextlib import redirect_stderr
        for arguments in (["unsupported-operation"], ["auth", "--credentials", "dummy", "--token", "dummy", "--unsupported-scope"],
                          ["workflow", "--env-auth", "--manifest", "dummy", "--output", "dummy", "--unsupported-action"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(arguments)
