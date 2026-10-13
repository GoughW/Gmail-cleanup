"""Drive download integrity is independent of post-upload metadata settling."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from gmail_cleanup.drive import BackupVerificationError, verify_file
from gmail_cleanup.engine import create, trash_original
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.mime import digest
from gmail_cleanup.preflight import run
from .helpers import Store
from .helpers import FakeDrive, FakeGmail, message


class DriveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive()
        self.drive.add("copy", b"Test", "destination", "Test.txt")
        self.hash = digest(b"Test")

    def mutate_on_download(self, update):
        download = self.drive.download_hash
        def mutate(key):
            result = download(key)
            update(self.drive.files[key])
            return result
        self.drive.download_hash = mutate

    def verify(self):
        return verify_file(self.drive, "copy", self.hash, 4, "destination",
                           expected_name="Test.txt", expected_parents=["destination"])

    def test_observed_four_byte_hash_and_version_one_to_three_pass(self):
        # The live evidence proves this hash and version transition, but did not
        # retain the complete two snapshots of the failed call.
        self.assertEqual(self.hash, "532eaabd9574880dbf76b9b8cc00832c20a6ec113d682299550d7a6e0f345e25")
        self.mutate_on_download(lambda item: item.update(version="3"))
        proof = self.verify()
        self.assertTrue(proof["verified"])
        self.assertEqual(proof['attempts'][0]['metadata_before']['version'], '1')
        self.assertEqual(proof["metadata_after"]["version"], "3")
        self.assertTrue(proof['checks']['unchanged_version_across_complete_download'])
        from gmail_cleanup.drive import metadata_changes
        self.assertEqual(metadata_changes(proof['attempts'][0]['metadata_before'], proof['attempts'][0]['metadata_after']), [{
            "field": "version", "before": "1", "after": "3",
            "classification": "benign/expected metadata mutation", "blocker": False}])

    def test_checksum_population_timestamps_and_binary_type_detection_are_benign(self):
        del self.drive.files["copy"]["sha256Checksum"]
        self.mutate_on_download(lambda item: item.update(
            version="3", mimeType="text/plain", sha256Checksum=self.hash,
            modifiedTime="2026-10-11T10:30:00Z", viewedByMeTime="2026-10-11T10:30:01Z",
            appProperties={"processing": "complete"}))
        proof = self.verify()
        self.assertTrue(proof["verified"])
        self.assertEqual(len(proof['attempts']), 2)
        self.assertNotIn('sha256Checksum', proof['attempts'][0]['metadata_before'])
        self.assertTrue(proof['checks']['unchanged_version_across_complete_download'])
        self.assertTrue(proof["advisory_checksum_matches_download"]["after"])

    def test_missing_version_and_contradictory_checksum_are_blockers(self):
        del self.drive.files["copy"]["version"]
        with self.assertRaises(BackupVerificationError):
            self.verify()
        self.drive.files['copy']['version'] = '1'
        self.drive.files["copy"]["sha256Checksum"] = "0" * 64
        with self.assertRaises(BackupVerificationError) as caught:
            self.verify()
        self.assertFalse(caught.exception.observation['checks']['populated_checksums_match_download'])

    def test_changed_integrity_fields_block_even_when_downloaded_hash_matches(self):
        for change in ({"id": "other"}, {"name": "renamed.txt"}, {"parents": ["elsewhere"]},
                       {"size": "5"}, {"size": None}, {"trashed": True},
                       {"mimeType": "application/vnd.google-apps.document"}):
            with self.subTest(change=change):
                self.setUp()
                self.mutate_on_download(lambda item: item.update(change))
                with self.assertRaises(BackupVerificationError) as caught:
                    self.verify()
                proof = caught.exception.observation
                self.assertFalse(proof["verified"])
                self.assertEqual(proof["downloaded_sha256"], self.hash)
                self.assertIn("metadata_before", proof)
                self.assertIn("metadata_after", proof)
                self.assertTrue(any(c["blocker"] for c in proof["metadata_changes"]))

    def test_stable_metadata_cannot_hide_corrupt_bytes(self):
        self.drive.corrupt_download = True
        with self.assertRaises(BackupVerificationError) as caught:
            self.verify()
        self.assertFalse(caught.exception.observation["checks"]["downloaded_sha256_matches_source"])
        self.assertEqual(caught.exception.observation["metadata_changes"], [])

    def test_same_size_content_change_is_not_hidden_by_a_benign_version_change(self):
        def change_content(item):
            self.drive.payloads["copy"] = b"evil"
            item.update(version="3", sha256Checksum=digest(b"evil"))
        self.mutate_on_download(change_content)
        with self.assertRaises(BackupVerificationError) as caught:
            self.verify()
        proof = caught.exception.observation
        self.assertEqual(proof["downloads"][0]["sha256"], self.hash)
        self.assertFalse(proof['checks']['populated_checksums_match_download'])
        self.assertFalse(proof['verified'])

    def test_wrong_name_or_destination_before_download_is_rejected(self):
        self.drive.files["copy"]["name"] = "wrong.txt"
        with self.assertRaises(BackupVerificationError):
            self.verify()
        self.assertEqual(self.drive.downloads, [])

    def test_disappearance_after_download_blocks_and_saves_both_reads(self):
        self.mutate_on_download(lambda item: self.drive.files.pop("copy"))
        with self.assertRaises(BackupVerificationError) as caught:
            self.verify()
        self.assertIsNone(caught.exception.observation["metadata_after"])
        self.assertTrue(caught.exception.observation["metadata_changes"][0]["blocker"])


class DriveGateRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        original, manifest = message()
        self.api = FakeGmail({"abc123": original})
        self.api.drive = FakeDrive()
        choices = [{"part": c["part"], "decoded_sha256": c["decoded_sha256"]}
                   for c in manifest["messages"]["abc123"]]
        selection = {"version": 2, "allowlist": ["abc123"], "messages": {"abc123": choices}}
        report, self.manifest = run(self.api, self.store, selection, self.api.drive, allow_upload=True)
        self.proof = report["results"][0]["selected_backups"][0]
        self.file_id = self.proof["file_id"]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_saved_filename_and_folder_expectations_block_later_renames_or_moves(self):
        for changes in ({"name": "renamed.txt"}, {"parents": ["wrong-folder"]}):
            with self.subTest(changes=changes):
                saved = deepcopy(self.api.drive.files[self.file_id])
                self.api.drive.files[self.file_id].update(changes)
                with self.assertRaises(BackupVerificationError):
                    create(self.api, self.store, self.manifest, "abc123", execute=True)
                self.assertEqual(self.api.inserts, [])
                self.api.drive.files[self.file_id] = saved

    def test_continual_version_changes_block_insert_and_trash(self):
        self.api.drive.change_during_download = True
        with self.assertRaises(BackupVerificationError):
            create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.assertEqual(self.api.inserts + self.api.trashes, [])

    def test_drive_rename_after_insert_blocks_trash(self):
        record = create(self.api, self.store, self.manifest, "abc123", execute=True)
        self.api.drive.files[self.file_id]["name"] = "renamed.txt"
        with self.assertRaises(CleanupError):
            trash_original(self.api, self.store, self.manifest, "abc123", execute=True,
                           enable_trash=True, approve_plan=record["plan"]["plan_sha256"],
                           approve_replacement=record["replacement_id"])
        self.assertEqual(self.api.trashes, [])
