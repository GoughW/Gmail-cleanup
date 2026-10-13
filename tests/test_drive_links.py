"""Canonical Drive viewer URLs survive backup, manifest and MIME note generation."""
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from gmail_cleanup.drive import Drive, FIELDS, verify_file, file_id_from_url, BackupVerificationError
from gmail_cleanup.engine import build_plan, create, plan_message, verify_after_trash
from gmail_cleanup.errors import CleanupError
from gmail_cleanup.mime import body_text, digest, parse, parts, validate_url
from gmail_cleanup.preflight import run
from .helpers import Store
from .helpers import FakeDrive, FakeGmail, message


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = {"href": dict(attrs).get("href"), "text": ""}
            self.links.append(self.current)

    def handle_data(self, text):
        if self.current is not None:
            self.current["text"] += text

    def handle_endtag(self, tag):
        if tag == "a":
            self.current = None


class DriveLinkTests(unittest.TestCase):
    def setUp(self):
        self.drive = FakeDrive()
        self.drive.add("copy", b"Test", name="Test.txt")
        self.url = "https://drive.google.com/file/d/copy/view?usp=drivesdk&resourcekey=0-example_key"
        self.drive.files["copy"]["webViewLink"] = self.url

    def proof(self):
        return verify_file(self.drive, "copy", digest(b"Test"), 4,
                           expected_name="Test.txt", expected_parents=["fixture-folder"])

    def test_download_verification_returns_exact_api_link_including_query(self):
        proof = self.proof()
        self.assertEqual(proof["drive_url"], self.url)
        self.assertEqual(proof["web_view_link"], self.url)
        self.assertEqual(proof["link_source"], "Drive files.webViewLink")
        self.assertTrue(proof["verified"])

    def test_web_view_link_settling_uses_latest_verified_metadata(self):
        del self.drive.files["copy"]["webViewLink"]
        original_download = self.drive.download_hash
        def download(file_id):
            result = original_download(file_id)
            self.drive.files[file_id]["webViewLink"] = self.url
            return result
        self.drive.download_hash = download
        proof = self.proof()
        self.assertEqual(proof["drive_url"], self.url)
        self.assertTrue(any(c["field"] == "webViewLink" and not c["blocker"]
                            for c in proof["metadata_changes"]))
        self.assertEqual(len(proof['downloads']), 1)
        self.assertTrue(proof['checks']['unchanged_version_across_complete_download'])

    def test_missing_unsafe_or_wrong_file_canonical_link_blocks_resolution(self):
        for link in (None, "", "http://drive.google.com/file/d/copy/view",
                     "https://evil.example/file/d/copy/view",
                     "https://drive.google.com/file/d/other/view",
                     "https://drive.google.com/open?id=copy&id=other"):
            with self.subTest(link=link):
                self.drive.files["copy"]["webViewLink"] = link
                with self.assertRaises(BackupVerificationError) as caught:
                    self.proof()
                self.assertFalse(caught.exception.observation["verified"])
                self.assertEqual(caught.exception.observation["downloaded_sha256"], digest(b"Test"))
                self.assertIsNone(caught.exception.observation["drive_url"])

    def test_drive_open_url_preserves_id_and_resource_key(self):
        self.url = "https://drive.google.com/open?id=copy&resourcekey=0-example_key&usp=drivesdk"
        self.drive.files["copy"]["webViewLink"] = self.url
        validate_url(self.url)
        self.assertEqual(file_id_from_url(self.url), "copy")
        self.assertEqual(self.proof()["drive_url"], self.url)

    def test_unsafe_viewer_routes_and_appended_punctuation_rejected(self):
        for url in ("https://drive.google.com/open?id=copy%2Fother",
                    "https://drive.google.com/open?id=",
                    "https://drive.google.com/file/d/copy/view;SHA-256:abc",
                    "https://drive.google.com/file/d/copy/view SHA-256:abc",
                    "https://drive.google.com/file/d/copy/view#misleading",
                    "https://user:secret@drive.google.com/open?id=copy"):
            with self.subTest(url=url), self.assertRaises(CleanupError):
                validate_url(url)

    def test_api_metadata_field_mask_requests_web_view_link(self):
        adapter = Drive.__new__(Drive)
        adapter.service = MagicMock()
        adapter.service.files.return_value.get.return_value.execute.return_value = self.drive.get("copy")
        self.assertEqual(adapter.get("copy")["webViewLink"], self.url)
        adapter.service.files.return_value.get.assert_called_once_with(fileId="copy", fields=FIELDS)
        self.assertIn("webViewLink", FIELDS.split(","))

    def test_preflight_persists_canonical_url_and_html_href_without_hash_punctuation(self):
        original, selections = message("html")
        api = FakeGmail({"abc123": original})
        selected = selections["messages"]["abc123"][0]
        file_id = file_id_from_url(selected["drive_url"])
        canonical = selected["drive_url"] + "?usp=drivesdk&resourcekey=0-example_key"
        api.drive.files[file_id]["webViewLink"] = canonical
        manifest = {"version": 2, "allowlist": ["abc123"], "messages": {
            "abc123": [{"part": selected["part"], "decoded_sha256": selected["decoded_sha256"],
                        "drive_file_id": file_id}]}}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "audit.sqlite")
            try:
                preview, resolved = run(api, store, manifest, api.drive)
                self.assertTrue(preview["all_ready_to_create"])
                self.assertEqual(resolved["messages"]["abc123"][0]["drive_url"], canonical)
                record = store.get("abc123")
                self.assertEqual(record["plan"]["selections"][0]["drive_url"], canonical)
                _, raw = build_plan(original, record["plan"]["selections"], record["plan"]["operation_id"])
                parsed = parse(raw)
                html = next(body_text(p) for _, p in parts(parsed) if p.get_content_type() == "text/html")
                plain = next(body_text(p) for _, p in parts(parsed) if p.get_content_type() == "text/plain")
                links = Links()
                links.feed(html)
                self.assertEqual(links.links, [{"href": canonical, "text": canonical}])
                self.assertIn(f'<a href="{escape(canonical, quote=True)}">{escape(canonical)}</a>; SHA-256:', html)
                self.assertIn("&amp;resourcekey=", html)
                self.assertNotIn("&amp;amp;", html)
                self.assertIn(f"Retained copy: {canonical}\r\nSHA-256:", plain)
                self.assertEqual(api.inserts + api.trashes + api.drive.creates, [])
            finally:
                store.close()

    def test_missing_api_link_preflight_never_creates_replacement_or_resolved_manifest(self):
        original, manifest = message()
        api = FakeGmail({"abc123": original})
        selected = manifest["messages"]["abc123"][0]
        file_id = file_id_from_url(selected["drive_url"])
        del api.drive.files[file_id]["webViewLink"]
        choices = {"version": 2, "allowlist": ["abc123"], "messages": {
            "abc123": [{"part": selected["part"], "decoded_sha256": selected["decoded_sha256"], "drive_file_id": file_id}]}}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "audit.sqlite")
            try:
                preview, resolved = run(api, store, choices, api.drive)
                self.assertFalse(preview["all_ready_to_create"])
                self.assertIsNone(resolved)
                self.assertEqual(api.inserts + api.trashes + api.drive.creates, [])
            finally:
                store.close()

    def test_legacy_approved_link_and_recovery_are_not_silently_rewritten(self):
        original, manifest = message()
        api = FakeGmail({"abc123": original})
        selected = manifest["messages"]["abc123"][0]
        file_id = file_id_from_url(selected["drive_url"])
        api.drive.files[file_id]["webViewLink"] = selected["drive_url"] + "?usp=drivesdk"
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "audit.sqlite")
            try:
                plan = plan_message(api, store, manifest, "abc123")["plan"]
                saved = deepcopy(plan)
                record = create(api, store, manifest, "abc123", execute=True)
                self.assertEqual(record["state"], "verified")
                self.assertEqual(record["plan"], saved)
                self.assertEqual(record["drive_verification"][0]["drive_url"], selected["drive_url"] + "?usp=drivesdk")
                # Recovery must still verify the original approved bytes after Trash.
                api.trash("abc123")
                record["state"] = "trash_pending"
                store.save(record, expected_state="verified")
                recovered = verify_after_trash(api, store, record)
                self.assertEqual(recovered["state"], "trashed")
                self.assertEqual(recovered["plan"], saved)
                self.assertEqual(len(api.inserts), 1)
            finally:
                store.close()
