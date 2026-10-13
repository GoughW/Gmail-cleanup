"""Regression for the live insert: two Received values become three."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from gmail_cleanup.engine import (build_plan, check_replacement, create, plan_message,
                                  trash_original, verify)
from gmail_cleanup.mime import parse, parts, SERVER_TRANSPORT_HEADERS
from .helpers import Store
from .helpers import FakeGmail, message


class ServerHeaderTests(unittest.TestCase):
    def prepared(self, case="inline"):
        original, manifest = message(case)
        parsed = parse(original["raw_bytes"])
        parsed["Received"] = "from first.example.invalid by second.example.invalid"
        parsed["Received"] = "from second.example.invalid by third.example.invalid"
        original["raw_bytes"] = parsed.as_bytes()
        plan, raw = build_plan(original, manifest["messages"]["abc123"], "approved-operation")
        replacement = deepcopy(original)
        replacement.update(id="replacement1", raw_bytes=raw)
        return original, manifest, plan, raw, replacement

    def add_header(self, replacement, name="Received", value="by Gmail after insert"):
        parsed = parse(replacement["raw_bytes"])
        parsed._headers.insert(0, (name, value))  # Gmail prepends the live Received header
        replacement["raw_bytes"] = parsed.as_bytes()

    def test_exact_live_case_audited_and_legacy_plan_unchanged(self):
        original, _, plan, raw, replacement = self.prepared()
        saved = deepcopy(plan)
        self.add_header(replacement)
        self.assertFalse(check_replacement(replacement, plan)["passed"])
        result = check_replacement(replacement, plan, raw, original["raw_bytes"])
        self.assertTrue(result["passed"], result)
        changes = result["differences"]["expected_to_observed_header_changes"]
        received = next(c for c in changes if c.get("name") == "received")
        self.assertEqual(len(received["before_value_sha256"]), 2)
        self.assertEqual(len(received["after_value_sha256"]), 3)
        self.assertEqual(received["classification"], "transport")
        self.assertEqual(plan, saved)
        self.assertTrue(result["differences"]["raw_bytes_differ"])
        self.assertTrue(any(c["part"] == "0.2" for c in result["differences"]["original_to_replacement_header_changes"]))

    def test_only_explicit_root_transport_metadata_is_tolerated(self):
        _, _, plan, raw, replacement = self.prepared()
        for name in SERVER_TRANSPORT_HEADERS:
            with self.subTest(name=name):
                copy = deepcopy(replacement)
                self.add_header(copy, name, "synthetic delivery assertion")
                self.assertTrue(check_replacement(copy, plan, raw)["passed"])
        self.add_header(replacement, "X-Unapproved-User-Header")
        self.assertFalse(check_replacement(replacement, plan, raw)["passed"])

    def test_transport_tolerance_does_not_hide_correspondence_or_threading_edits(self):
        _, _, plan, raw, replacement = self.prepared()
        for name, value in {"From": "different@example.invalid", "To": "different@example.invalid",
                            "Cc": "different@example.invalid", "Subject": "changed",
                            "Date": "Thu, 08 Oct 2026 12:34:56 +0000",
                            "Message-ID": "<changed@example.invalid>",
                            "In-Reply-To": "<changed@example.invalid>",
                            "References": "<changed@example.invalid>"}.items():
            with self.subTest(name=name):
                copy = deepcopy(replacement)
                parsed = parse(copy["raw_bytes"])
                del parsed[name]
                parsed[name] = value
                copy["raw_bytes"] = parsed.as_bytes()
                self.add_header(copy)
                self.assertFalse(check_replacement(copy, plan, raw)["passed"])
        for field, value in {"internalDate": "0", "threadId": "different", "labelIds": []}.items():
            with self.subTest(field=field):
                copy = deepcopy(replacement)
                copy[field] = value
                self.add_header(copy)
                self.assertFalse(check_replacement(copy, plan, raw)["passed"])

    def test_decoded_bodies_inline_and_unselected_attachments_remain_strict(self):
        original, _, plan, raw, replacement = self.prepared()
        for path, part in parts(parse(raw)):
            if part.is_multipart():
                continue
            with self.subTest(path=path):
                copy = deepcopy(replacement)
                parsed = parse(raw)
                leaf = dict(parts(parsed))[path]
                leaf.set_payload("Y2hhbmdlZA==")
                copy["raw_bytes"] = parsed.as_bytes()
                self.add_header(copy)
                self.assertFalse(check_replacement(copy, plan, raw)["passed"])
        parsed = parse(raw)
        dict(parts(parsed))["0.0"]["Received"] = "nested transport header is not exempt"
        replacement["raw_bytes"] = parsed.as_bytes()
        self.assertFalse(check_replacement(replacement, plan, raw)["passed"])
        replacement["raw_bytes"] = original["raw_bytes"]
        self.add_header(replacement)
        self.assertFalse(check_replacement(replacement, plan, raw)["passed"])

    def test_unapproved_reference_cannot_weaken_verification(self):
        _, _, plan, raw, replacement = self.prepared()
        parsed = parse(raw)
        parsed.replace_header("Subject", "unapproved")
        replacement["raw_bytes"] = parsed.as_bytes()
        self.assertFalse(check_replacement(replacement, plan, replacement["raw_bytes"])["passed"])

    def test_existing_failed_replacement_reverified_then_trashed_without_reinsert(self):
        original, manifest, _, _, _ = self.prepared("plain")
        api = FakeGmail({"abc123": original})
        api.corrupt = self.add_header
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "audit.sqlite")
            try:
                planned = plan_message(api, store, manifest, "abc123")
                # Model the prior strict verifier's persisted failure and known ID.
                _, raw = build_plan(original, planned["plan"]["selections"], planned["plan"]["operation_id"])
                from gmail_cleanup.engine import mutation_binding
                planned['insert_intent_key'] = store.intent('gmail-insert', mutation_binding(planned))
                response = api.insert(raw, planned["plan"]["original"]["labels"], original["threadId"])
                planned.update(state="verify_failed", replacement_id=response["id"], verification={"passed": False})
                store.save(planned, expected_state="planned")
                verified = verify(api, store, planned)
                self.assertEqual(verified["state"], "verified")
                trashed = trash_original(api, store, manifest, "abc123", execute=True,
                    enable_trash=True, approve_plan=verified["plan"]["plan_sha256"],
                    approve_replacement=response["id"])
                self.assertEqual(trashed["state"], "trashed")
                self.assertEqual(len(api.inserts), 1)
                self.assertEqual(api.trashes, ["abc123"])
                self.assertTrue(trashed["post_trash_verification"]["differences"]["expected_to_observed_header_changes"])
            finally:
                store.close()
