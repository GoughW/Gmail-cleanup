from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
import types
from unittest.mock import MagicMock, patch
from contextlib import redirect_stdout

from gmail_cleanup.api import Gmail, DRIVE_SCOPE, MODIFY_SCOPE
from gmail_cleanup.cli import main
from gmail_cleanup.drive import Backups, Drive, FOLDER_TYPE, verify_file, verify_selections, BackupVerificationError
from gmail_cleanup.engine import build_plan, check_replacement, create, plan_message, trash_original
from gmail_cleanup.errors import CleanupError, Unsupported
from gmail_cleanup.manifest import load_backup_manifest
from gmail_cleanup.mime import (TRANSPORT_AUTH_HEADERS, digest, parse, parts, transform,
                               transport_header_changes, header_fingerprint, without_transport_auth)
from gmail_cleanup.preflight import run
from gmail_cleanup.storage import write_private_json
from .helpers import Store
from gmail_cleanup.fixtures import CASES
from .helpers import FakeDrive, FakeGmail, message


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "audit.sqlite")
        self.drive = FakeDrive()
        self.backups = Backups(self.drive, self.store)
        self.payload = b"exact retained bytes \x00\xff"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_default_never_creates_any_drive_file(self):
        with self.assertRaises(CleanupError):
            self.backups.ensure(self.payload, "file.txt")
        self.assertEqual(self.drive.creates, [])
        self.assertEqual(self.drive.allocated, [])

    def test_upload_download_and_repeat_deduplicate_equal_bytes_with_different_names(self):
        first = self.backups.ensure(self.payload, "duplicate.txt", allow_upload=True)
        second = self.backups.ensure(self.payload, "résumé.txt", allow_upload=True)
        self.assertEqual(first, second)
        self.assertTrue(first["verified"])
        self.assertEqual(first["source_sha256"], digest(self.payload))
        self.assertEqual(first["downloaded_sha256"], digest(self.payload))
        self.assertEqual(len(self.drive.creates), 2)  # folder plus file
        self.assertEqual(len(self.drive.downloads), 2)
        key = first["parents"][0] + ":" + digest(self.payload)
        self.assertEqual(self.store.get_backup(key)["state"], "verified")

    def test_find_tagged_copy_downloads_instead_of_trusting_metadata(self):
        self.drive.add("folder")
        self.drive.add("existing", self.payload, "folder")
        result = self.backups.ensure(self.payload, "other-name.txt", "folder")
        self.assertEqual(result["file_id"], "existing")
        self.assertEqual(self.drive.creates, [])
        self.assertEqual(self.drive.downloads, ["existing"])

    def test_explicit_file_id_verifies_untagged_existing_copy(self):
        self.drive.add("existing", self.payload)
        self.drive.files["existing"]["appProperties"] = {}
        proof = self.backups.ensure(self.payload, "name.txt", file_id="existing")
        self.assertTrue(proof["verified"])
        self.assertEqual(self.drive.creates, [])

    def test_corrupt_tagged_copy_is_not_hidden_with_another_upload(self):
        self.drive.add("folder")
        self.drive.add("existing", self.payload, "folder")
        self.drive.corrupt_download = True
        with self.assertRaises(CleanupError):
            self.backups.ensure(self.payload, "name.txt", "folder", allow_upload=True)
        self.assertEqual(self.drive.creates, [])

    def test_continual_version_counter_change_blocks_matching_payload(self):
        self.drive.add("existing", self.payload)
        self.drive.change_during_download = True
        with self.assertRaises(BackupVerificationError) as caught:
            verify_file(self.drive, 'existing', digest(self.payload))
        self.assertFalse(caught.exception.observation['verified'])
        self.assertEqual(len(caught.exception.observation['attempts']), 2)

    def test_missing_trashed_native_wrong_parent_or_wrong_metadata_fail(self):
        self.drive.add("existing", self.payload)
        base = deepcopy(self.drive.files["existing"])
        mutations = [{"trashed": True}, {"mimeType": "application/vnd.google-apps.document"},
                     {"size": "999"}, {"id": "wrong"}, {"name": ""}, {"parents": None}]
        for changes in mutations:
            self.drive.files["existing"] = {**base, **changes}
            with self.subTest(changes=changes), self.assertRaises(CleanupError):
                verify_file(self.drive, "existing", digest(self.payload))
        self.drive.files["existing"] = base
        with self.assertRaises(CleanupError):
            verify_file(self.drive, "existing", digest(self.payload), folder_id="wrong")
        with self.assertRaises(CleanupError):
            verify_file(self.drive, "missing", digest(self.payload))

    def test_lost_create_response_recovers_using_allocated_id(self):
        self.drive.lose_response = True
        first = self.backups.ensure(self.payload, "first.txt", allow_upload=True)
        second = self.backups.ensure(self.payload, "first.txt", allow_upload=True)
        self.assertEqual(first, second)
        self.assertEqual(len(self.drive.creates), 2)

    def test_crash_after_upload_before_confirmation_resumes_without_duplicate(self):
        self.drive.add("folder")
        save = self.store.save_backup
        def crash(key, record, **kwargs):
            if record["state"] == "stored":
                raise RuntimeError("process interrupted after remote write")
            return save(key, record, **kwargs)
        self.store.save_backup = crash
        with self.assertRaises(RuntimeError):
            self.backups.ensure(self.payload, "name.txt", "folder", allow_upload=True)
        self.store.save_backup = save
        key = "folder:" + digest(self.payload)
        self.assertEqual(self.store.get_backup(key)["state"], "upload_pending")
        proof = self.backups.ensure(self.payload, "name.txt", "folder", allow_upload=True)
        self.assertTrue(proof["verified"])
        self.assertEqual(len(self.drive.creates), 1)

    def test_rejected_upload_retries_same_preallocated_id_after_restart(self):
        self.drive.add("folder")
        self.drive.reject_create = True
        with self.assertRaises(CleanupError):
            self.backups.ensure(self.payload, "name.txt", "folder", allow_upload=True)
        key = "folder:" + digest(self.payload)
        allocated = self.store.get_backup(key)["file_id"]
        self.store.close()
        self.store = Store(Path(self.temp.name) / "audit.sqlite")
        self.backups = Backups(self.drive, self.store)
        self.drive.reject_create = False
        with self.assertRaises(CleanupError):
            self.backups.ensure(self.payload, 'name.txt', 'folder', allow_upload=True)
        self.backups = Backups(self.drive, self.store, recover_upload=True)
        proof = self.backups.ensure(self.payload, "name.txt", "folder", allow_upload=True)
        self.assertEqual(proof["file_id"], allocated)
        self.assertEqual(len(self.drive.allocated), 1)

    def test_missing_previously_verified_file_is_not_recreated(self):
        proof = self.backups.ensure(self.payload, "name.txt", allow_upload=True)
        del self.drive.files[proof["file_id"]]
        with self.assertRaises(CleanupError):
            self.backups.ensure(self.payload, "name.txt", allow_upload=True)
        self.assertEqual(len(self.drive.creates), 2)

    def test_ambiguous_folders_and_counterparts_fail_closed(self):
        self.drive.add("folder-a")
        self.drive.add("folder-b")
        with self.assertRaises(CleanupError):
            self.backups.folder()
        self.drive.add("copy-a", self.payload, "folder-a")
        self.drive.add("copy-b", self.payload, "folder-a")
        with self.assertRaises(CleanupError):
            self.backups.ensure(self.payload, "name.txt", "folder-a")

    def test_gateway_does_not_initialize_drive_without_explicit_oauth_scope(self):
        credentials = MagicMock(granted_scopes=[MODIFY_SCOPE], scopes=[MODIFY_SCOPE])
        with self.assertRaises(CleanupError):
            Drive(credentials, MagicMock())

    def test_no_delete_update_or_permissions_api_surface_exists(self):
        self.assertFalse(any(hasattr(Drive, name) for name in ("delete", "trash", "update", "share", "send")))

    def test_api_failures_are_sanitized_and_not_automatically_retried(self):
        request = MagicMock()
        request.execute.side_effect = ValueError("SECRET TOKEN AND RAW FILE")
        with self.assertRaises(CleanupError) as exc:
            Drive._execute(request)
        self.assertNotIn("SECRET", str(exc.exception))
        request.execute.assert_called_once_with(num_retries=0)

    def test_download_adapter_hashes_every_chunk_with_no_disk_write(self):
        module = types.ModuleType("googleapiclient.http")
        chunks = [b"first\x00", b"second\xff"]
        instances = []
        class Downloader:
            def __init__(self, sink, request, chunksize):
                self.sink = sink
                self.calls = []
                self.pending = list(chunks)
                instances.append(self)
            def next_chunk(self, num_retries):
                self.calls.append(num_retries)
                self.sink.write(self.pending.pop(0))
                return None, not self.pending
        module.MediaIoBaseDownload = Downloader
        adapter = Drive.__new__(Drive)
        adapter.service = MagicMock()
        with patch.dict("sys.modules", {"googleapiclient.http": module}):
            actual, size = adapter.download_hash("binary-file")
        self.assertEqual(actual, digest(b"".join(chunks)))
        self.assertEqual(size, sum(map(len, chunks)))
        self.assertEqual(instances[0].calls, [0, 0])
        adapter.service.files.return_value.get_media.assert_called_once_with(fileId="binary-file")


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "audit.sqlite")
        self.original, self.manifest = message("inline")
        self.api = FakeGmail({"abc123": self.original})
        self.backup_manifest = {"version": 2, "allowlist": ["abc123"], "messages": {
            "abc123": [{k: v for k, v in self.manifest["messages"]["abc123"][0].items()
                        if k in {"part", "decoded_sha256"}}]}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_preview_uploads_and_verifies_drive_without_any_gmail_write(self):
        report, resolved = run(self.api, self.store, self.backup_manifest, self.api.drive, allow_upload=True)
        row = report["results"][0]
        self.assertTrue(report["all_ready_to_create"])
        self.assertTrue(row["local_reconstruction_verified"])
        self.assertTrue(row["original_unchanged"])
        self.assertTrue(row["selected_backups"][0]["drive_verified"])
        self.assertEqual(self.api.inserts + self.api.trashes, [])
        self.assertEqual(self.api.messages["abc123"], self.original)
        self.assertEqual(self.store.get("abc123")["state"], "planned")
        # A later create re-downloads the Drive bytes immediately before insertion.
        downloads = len(self.api.drive.downloads)
        record = create(self.api, self.store, resolved, "abc123", execute=True)
        self.assertEqual(record["state"], "verified")
        self.assertGreater(len(self.api.drive.downloads), downloads)

    def test_missing_drive_access_reports_local_success_but_creation_blocked(self):
        report, resolved = run(self.api, self.store, self.backup_manifest, drive_error="Drive grant missing")
        self.assertFalse(report["all_ready_to_create"])
        self.assertIsNone(resolved)
        self.assertTrue(report["results"][0]["local_reconstruction_verified"])
        self.assertFalse(report["results"][0]["selected_backups"][0]["drive_verified"])
        self.assertIsNone(self.store.get("abc123"))
        self.assertEqual(self.api.inserts + self.api.trashes, [])

    def test_all_fixture_cases_backup_and_reconstruct_without_touching_originals(self):
        messages, choices = {}, {}
        for index, case in enumerate(CASES):
            key = f"abc{index}"
            item, original_manifest = message(case, message_id=key)
            messages[key] = item
            choices[key] = [{"part": c["part"], "decoded_sha256": c["decoded_sha256"]}
                            for c in original_manifest["messages"][key]]
        api = FakeGmail(messages)
        api.drive = FakeDrive()  # begin with an empty app-scoped backup destination
        manifest = {"version": 2, "allowlist": list(messages), "messages": choices}
        report, resolved = run(api, self.store, manifest, api.drive, allow_upload=True)
        self.assertTrue(report["all_ready_to_create"])
        self.assertEqual(len(report["results"]), len(CASES))
        self.assertEqual(api.messages, messages)
        self.assertEqual(api.inserts + api.trashes, [])
        for row in report["results"]:
            self.assertTrue(row["local_reconstruction_verified"])
            self.assertTrue(row["selected_backups"][0]["drive_verified"])
            self.assertEqual(self.store.get(row["message_id"])["plan"]["expected"]["headers_sha256"],
                             row["reconstructed_headers_sha256"])

    def test_drive_failure_is_persisted_with_actual_download_hash(self):
        self.api.drive.corrupt_download = True
        report, _ = run(self.api, self.store, self.backup_manifest, self.api.drive,
                        folder_id="fixture-folder", allow_upload=True)
        backup = report["results"][0]["selected_backups"][0]
        self.assertFalse(backup["drive_verified"])
        self.assertNotEqual(backup["source_sha256"], backup["downloaded_sha256"])
        key = "fixture-folder:" + backup["source_sha256"]
        record = self.store.get_backup(key)
        self.assertEqual(record["state"], "verify_failed")
        self.assertEqual(record["verification"]["downloaded_sha256"], backup["downloaded_sha256"])

    def test_original_changes_before_plan_persistence_block_readiness(self):
        get = self.api.get
        reads = 0
        def change(key):
            nonlocal reads
            reads += 1
            if reads == 4:  # final planning fetch, after earlier before/after comparisons
                self.api.messages[key]["labelIds"].remove("UNREAD")
            return get(key)
        self.api.get = change
        report, resolved = run(self.api, self.store, self.backup_manifest, self.api.drive, allow_upload=True)
        self.assertFalse(report["all_ready_to_create"])
        self.assertIsNone(resolved)
        self.assertIsNone(self.store.get("abc123"))
        self.assertEqual(self.api.inserts + self.api.trashes, [])

    def test_upload_is_separately_gated_even_when_scope_exists(self):
        self.api.drive = FakeDrive()
        report, _ = run(self.api, self.store, self.backup_manifest, self.api.drive)
        self.assertFalse(report["all_ready_to_create"])
        self.assertEqual(self.api.drive.creates, [])

    def test_unsupported_signed_mime_does_not_upload(self):
        msg = parse(self.original["raw_bytes"])
        msg.set_type("multipart/signed")
        self.api.messages["abc123"]["raw_bytes"] = msg.as_bytes()
        report, _ = run(self.api, self.store, self.backup_manifest, self.api.drive, allow_upload=True)
        self.assertFalse(report["all_ready_to_create"])
        self.assertEqual(self.api.drive.creates, [])
        self.assertIn("Signed/encrypted", report["results"][0]["error"])

    def test_source_hash_mismatch_fails_before_upload(self):
        self.backup_manifest["messages"]["abc123"][0]["decoded_sha256"] = "0" * 64
        report, _ = run(self.api, self.store, self.backup_manifest, self.api.drive, allow_upload=True)
        self.assertFalse(report["all_ready_to_create"])
        self.assertEqual(self.api.drive.creates, [])

    def test_only_allowlisted_ids_are_read_and_processed(self):
        other, _ = message(message_id="def456")
        self.api.messages["def456"] = other
        run(self.api, self.store, self.backup_manifest, self.api.drive, allow_upload=True)
        self.assertEqual(set(self.api.reads), {"abc123"})

    def test_manifest_rejects_invalid_scope_ids_and_extra_selection_fields(self):
        path = self.root / "manifest.json"
        write_private_json(path, self.backup_manifest)
        self.assertEqual(load_backup_manifest(path), self.backup_manifest)
        for change in ({"drive_file_id": "a' or trashed=true"}, {"filename": "remove.bin"}):
            bad = deepcopy(self.backup_manifest)
            bad["messages"]["abc123"][0].update(change)
            write_private_json(path, bad)
            with self.assertRaises(CleanupError):
                load_backup_manifest(path)

    def test_create_cannot_use_a_fabricated_retained_hash_without_drive(self):
        plan_message(self.api, self.store, self.manifest, "abc123")
        self.api.drive = None
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(self.api.inserts, [])
        self.assertEqual(self.store.get("abc123")["state"], "planned")

    def test_preview_detects_uncertain_mailbox_insert_before_any_drive_creation(self):
        record = plan_message(self.api, self.store, self.manifest, "abc123")
        record["state"] = "insert_pending"
        self.store.save(record, expected_state="planned")
        report, resolved = run(self.api, self.store, self.backup_manifest, self.api.drive, allow_upload=True)
        self.assertFalse(report["all_ready_to_create"])
        self.assertIsNone(resolved)
        self.assertIn("pending/completed", report["results"][0]["error"])
        self.assertEqual(self.api.drive.creates, [])
        self.assertEqual(self.api.inserts + self.api.trashes, [])

    def test_drive_changes_after_plan_block_create_and_leave_original_intact(self):
        plan_message(self.api, self.store, self.manifest, "abc123")
        self.api.drive.corrupt_download = True
        with self.assertRaises(CleanupError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(self.api.inserts + self.api.trashes, [])
        self.assertEqual(self.api.messages["abc123"], self.original)

    def test_drive_changes_after_replacement_block_trash(self):
        plan_message(self.api, self.store, self.manifest, "abc123")
        record = create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.api.drive.corrupt_download = True
        with self.assertRaises(CleanupError):
            trash_original(self.api, self.store, self.manifest, "abc123", execute=True, enable_trash=True,
                           approve_plan=record["plan"]["plan_sha256"], approve_replacement=record["replacement_id"])
        self.assertEqual(self.api.trashes, [])
        self.assertEqual(self.store.get("abc123")["state"], "verify_failed")

    def test_cli_preview_has_no_write_enabled_gmail_client_and_private_report(self):
        path, output = self.root / "manifest.json", self.root / "report.json"
        write_private_json(path, self.backup_manifest)
        stdout = io.StringIO()
        with patch("gmail_cleanup.cli.Gmail.from_environment", return_value=self.api) as factory, redirect_stdout(stdout):
            code = main(["preflight", "--env-auth", "--manifest", str(path), "--output", str(output),
                         "--state", str(self.root / "cli.sqlite"), "--upload-backups", '--initialize-state', '--mailbox-key', 'fake-mailbox'])
        factory.assert_called_once_with(require_modify=False)
        self.assertEqual(code, 0)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("drive.google.com", stdout.getvalue())
        self.assertEqual(self.api.inserts + self.api.trashes, [])


class TransportTests(unittest.TestCase):
    def test_unreferenced_cid_attachment_is_protected_and_retained_when_another_is_removed(self):
        original, manifest = message("inline")
        msg = parse(original["raw_bytes"])
        attachment = dict(parts(msg))[manifest["messages"]["abc123"][0]["part"]]
        attachment["Content-ID"] = "<normal-gmail-attachment>"
        raw = msg.as_bytes()
        with self.assertRaises(Unsupported):
            transform(raw, manifest['messages']['abc123'], 'operation')
        # The compatibility restriction must not damage unselected resources.
        other = dict(parts(msg))['0.2']
        choices = [{**manifest['messages']['abc123'][0], 'part': '0.2',
                    'decoded_sha256': digest(other.get_payload(decode=True)),
                    'retained_sha256': digest(other.get_payload(decode=True))}]
        _, _, result = transform(raw, choices, 'operation')
        before_images = [(str(p["Content-ID"]), digest(p.get_payload(decode=True)))
                         for _, p in parts(msg) if p.get_content_maintype() == "image"]
        after_images = [(str(p["Content-ID"]), digest(p.get_payload(decode=True)))
                        for _, p in parts(result) if p.get_content_maintype() == "image"]
        self.assertEqual(before_images, after_images)
        retained = next(p for _,p in parts(result) if p.get('Content-ID') == '<normal-gmail-attachment>')
        self.assertEqual(retained.get_payload(decode=True), attachment.get_payload(decode=True))

    def test_referenced_attachment_cid_cannot_be_removed_even_if_disposition_says_attachment(self):
        for uri in ("cid:protected", "cid%3Aprotected", "cid&#58;protected"):
            original, manifest = message("html")
            msg = parse(original["raw_bytes"])
            dict(parts(msg))[manifest["messages"]["abc123"][0]["part"]]["Content-ID"] = "<protected>"
            html = next(p for _, p in parts(msg) if p.get_content_type() == "text/html")
            html.set_content(f'<img src="{uri}">', subtype="html")
            with self.subTest(uri=uri), self.assertRaises(Unsupported):
                transform(msg.as_bytes(), manifest["messages"]["abc123"], "operation")

    def test_complete_repeated_arc_chains_removed_and_audited_without_values(self):
        original, manifest = message()
        msg = parse(original["raw_bytes"])
        for name in TRANSPORT_AUTH_HEADERS:
            msg[name] = "synthetic delivery assertion"
        msg["ARC-Seal"] = "second ARC hop"
        raw = msg.as_bytes()
        source = parse(raw)
        modified_raw, _, modified = transform(raw, manifest["messages"]["abc123"], "operation")
        changes = transport_header_changes(source)
        self.assertEqual(len(changes), len(TRANSPORT_AUTH_HEADERS) + 1)
        self.assertTrue(all(c["value_sha256"] and "value" not in c for c in changes))
        self.assertFalse(set(k.lower() for k in modified.keys()) & TRANSPORT_AUTH_HEADERS)
        for header in ("From", "To", "Subject", "Date", "Message-ID"):
            self.assertEqual(modified[header], source[header])
        original["raw_bytes"] = raw
        plan, _ = build_plan(original, manifest["messages"]["abc123"], "operation")
        self.assertEqual(plan["transport_headers_removed"], changes)
        # Reintroducing a delivery signature must fail exact reconstruction verification.
        modified["DKIM-Signature"] = "unexpected signature"
        replacement = {**original, "id": "replacement", "raw_bytes": modified.as_bytes()}
        result = check_replacement(replacement, plan)
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["correspondence_and_threading_headers"])

    def test_transport_headers_inside_mime_leaves_are_unsupported(self):
        original, manifest = message()
        parsed = parse(original["raw_bytes"])
        parsed.get_payload()[1]["DKIM-Signature"] = "ambiguous part signature"
        with self.assertRaises(Unsupported):
            transform(parsed.as_bytes(), manifest["messages"]["abc123"], "operation")
