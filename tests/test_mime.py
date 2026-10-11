from copy import deepcopy
from email import policy
from email.message import EmailMessage
import unittest

from gmail_cleanup.errors import CleanupError, Unsupported
from gmail_cleanup.fixtures import CASES, fixture
from gmail_cleanup.mime import (MARKER, body, body_text, digest, header_fingerprint,
                               inventory, parse, parts, transform)


class MimeTests(unittest.TestCase):
    def test_all_fixture_variants_preserve_correspondence_and_unselected_payloads(self):
        for case in CASES:
            with self.subTest(case=case):
                raw, selections = fixture(case)
                original = parse(raw)
                result, removed, modified = transform(raw, selections, "operation123")
                self.assertEqual(removed[0]["decoded_sha256"], selections[0]["retained_sha256"])
                self.assertEqual(modified.get_all(MARKER), ["operation123"])
                for header in ("From", "To", "Cc", "Subject", "Date", "Message-ID", "References", "In-Reply-To"):
                    self.assertEqual(original.get_all(header), modified.get_all(header))
                original_bodies = {p.get_content_type(): body_text(p) for _, p in parts(original) if body(p)}
                new_bodies = {p.get_content_type(): body_text(p) for _, p in parts(modified) if body(p)}
                self.assertEqual(set(original_bodies), set(new_bodies))
                for kind, text in original_bodies.items():
                    self.assertTrue(new_bodies[kind].startswith(text))
                    self.assertIn(removed[0]["drive_url"], new_bodies[kind])
                    self.assertIn("Café", new_bodies[kind])
                # Compare all unselected binary parts, including inline resources;
                # paths may shift when an earlier sibling was removed.
                selected = {s["part"] for s in selections}
                remaining = [(p.get_content_type(), p.get_filename(), str(p.get("Content-ID")),
                              digest(p.get_payload(decode=True)))
                             for path, p in parts(original) if not p.is_multipart() and not body(p) and path not in selected]
                actual = [(p.get_content_type(), p.get_filename(), str(p.get("Content-ID")),
                           digest(p.get_payload(decode=True)))
                          for _, p in parts(modified) if not p.is_multipart() and not body(p)]
                self.assertEqual(remaining, actual)
                self.assertEqual(inventory(parse(result)), inventory(modified))
                if case == "large":
                    self.assertGreater(len(raw) - len(result), 4 * 1024 * 1024)

    def test_duplicate_filenames_select_by_part_and_hash(self):
        raw, selections = fixture("duplicate")
        original = parse(raw)
        self.assertEqual([p.get_filename() for _, p in parts(original) if p.get_filename()], ["same.bin", "same.bin"])
        result, _, _ = transform(raw, selections, "op")
        remaining = [p for _, p in parts(parse(result)) if p.get_content_disposition() == "attachment"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].get_payload(decode=True), b"keep this distinct synthetic attachment")

    def test_identical_attachment_bytes_still_remove_only_the_selected_part(self):
        raw, choices = fixture()
        msg = parse(raw)
        msg.attach(deepcopy(msg.get_payload()[1]))
        _, removed, changed = transform(msg.as_bytes(), choices, "op")
        self.assertEqual(len(removed), 1)
        attachments = [p for _, p in parts(changed) if p.get_content_disposition() == "attachment"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(digest(attachments[0].get_payload(decode=True)), choices[0]["decoded_sha256"])

    def test_unicode_addresses_subject_and_all_correspondence_headers_survive(self):
        raw, choices = fixture()
        msg = parse(raw)
        msg.replace_header("Subject", "Résumé – 文書 🙂")
        msg.replace_header("From", "Café 東京 <sender@example.invalid>")
        msg.replace_header("To", "Renée <recipient@example.invalid>")
        msg["Bcc"] = "hidden@example.invalid"
        msg["Reply-To"] = "reply@example.invalid"
        msg["Sender"] = "delegated@example.invalid"
        msg["Return-Path"] = "<bounce@example.invalid>"
        msg["Received"] = "from synthetic.example.invalid by fixture.example.invalid; Wed, 07 Oct 2026 12:34:56 +0000"
        original = parse(msg.as_bytes())
        _, _, result = transform(msg.as_bytes(), choices, "op")
        for name in ("Subject", "From", "To", "Bcc", "Reply-To", "Sender", "Return-Path", "Received"):
            self.assertEqual(original.get_all(name), result.get_all(name))

    def test_hash_selection_and_retained_hash_each_must_match(self):
        raw, choices = fixture()
        for key in ("decoded_sha256", "retained_sha256"):
            bad = deepcopy(choices)
            bad[0][key] = "0" * 64
            with self.subTest(key=key), self.assertRaises(CleanupError):
                transform(raw, bad, "op")

    def test_part_identity_is_required_and_inline_images_cannot_be_selected(self):
        raw, choices = fixture("inline")
        original = parse(raw)
        inline_path, image = next((path, p) for path, p in parts(original) if p.get("Content-ID"))
        choices[0].update(part=inline_path, decoded_sha256=digest(image.get_payload(decode=True)),
                          retained_sha256=digest(image.get_payload(decode=True)))
        with self.assertRaises(Unsupported):
            transform(raw, choices, "op")
        choices[0]["part"] = "0.99"
        with self.assertRaises(CleanupError):
            transform(raw, choices, "op")

    def test_repeated_selection_is_rejected(self):
        raw, choices = fixture()
        with self.assertRaises(CleanupError):
            transform(raw, choices + choices, "op")

    def test_transport_signatures_are_removed(self):
        for header, value in [("DKIM-Signature", "v=1; a=rsa-sha256; b=synthetic"),
                              ("ARC-Seal", "i=1; b=synthetic")]:
            raw, choices = fixture()
            msg = parse(raw)
            msg[header] = value
            with self.subTest(header=header):
                _, _, result = transform(msg.as_bytes(), choices, "op")
                self.assertIsNone(result.get(header))
                self.assertEqual(result["Message-ID"], msg["Message-ID"])

    def test_signed_and_encrypted_variants_are_skipped(self):
        for kind in ("multipart/signed", "multipart/encrypted", "application/pkcs7-mime"):
            raw, choices = fixture()
            msg = parse(raw)
            if kind.startswith("application/"):
                dict(parts(msg))[choices[0]["part"]].set_type(kind)
            else:
                msg.set_type(kind)
            with self.subTest(kind=kind), self.assertRaises(Unsupported):
                transform(msg.as_bytes(), choices, "op")

    def test_inline_pgp_signed_and_encrypted_bodies_are_skipped(self):
        for marker in ("-----BEGIN PGP MESSAGE-----", "-----BEGIN PGP SIGNED MESSAGE-----"):
            raw, choices = fixture()
            msg = parse(raw)
            msg.get_payload()[0].set_content(marker + "\nsynthetic armored content\n")
            with self.subTest(marker=marker), self.assertRaises(Unsupported):
                transform(msg.as_bytes(), choices, "op")

    def test_multiple_explicit_attachment_selections_are_all_recorded(self):
        raw, choices = fixture("multiple")
        original = parse(raw)
        second_path, second = [(path, p) for path, p in parts(original)
                               if p.get_content_disposition() == "attachment"][1]
        second_hash = digest(second.get_payload(decode=True))
        choices.append({"part": second_path, "decoded_sha256": second_hash, "retained_sha256": second_hash,
                        "drive_url": "https://drive.google.com/file/d/SYNTHETIC-SECOND/view"})
        _, removed, changed = transform(raw, choices, "op")
        self.assertEqual(len(removed), 2)
        self.assertFalse(any(p.get_content_disposition() == "attachment" for _, p in parts(changed)))
        text = body_text(changed.get_payload()[0])
        for item in removed:
            self.assertIn(item["filename"], text)
            self.assertIn(item["drive_url"], text)

    def test_attached_nested_email_is_skipped(self):
        raw, choices = fixture()
        msg = parse(raw)
        nested = EmailMessage(policy=policy.SMTP)
        nested.set_content("Nested synthetic message")
        msg.add_attachment(nested, filename="forward.eml")
        with self.assertRaises(Unsupported):
            transform(msg.as_bytes(), choices, "op")

    def test_ambiguous_bodies_duplicate_headers_and_broken_boundaries_are_skipped(self):
        raw, choices = fixture()
        msg = parse(raw)
        msg._headers.append(("Subject", "duplicate"))
        with self.assertRaises(Unsupported):
            transform(msg.as_bytes(), choices, "op")
        msg = parse(raw)
        extra = EmailMessage(policy=policy.SMTP)
        extra.set_content("Second competing body")
        msg.attach(extra)
        with self.assertRaises(Unsupported):
            transform(msg.as_bytes(), choices, "op")
        with self.assertRaises(Unsupported):
            transform(raw.replace(b"--gmail-cleanup-offline-plain-0--", b"--incorrect--"), choices, "op")

    def test_lossy_charset_is_rejected(self):
        raw, choices = fixture()
        msg = parse(raw)
        msg.get_payload()[0].set_param("charset", "unknown-synthetic-charset")
        with self.assertRaises(Unsupported):
            transform(msg.as_bytes(), choices, "op")

    def test_legacy_charset_and_body_language_are_preserved(self):
        raw, choices = fixture()
        msg = parse(raw)
        plain = msg.get_payload()[0]
        plain.set_content("Café correspondence\n", charset="iso-8859-1", cte="base64")
        plain["Content-Language"] = "fr"
        _, _, changed = transform(msg.as_bytes(), choices, "op")
        self.assertTrue(body_text(changed.get_payload()[0]).startswith("Café correspondence\r\n"))
        self.assertEqual(changed.get_payload()[0]["Content-Language"], "fr")

    def test_html_note_escapes_filename_and_url(self):
        raw, choices = fixture("html")
        msg = parse(raw)
        target = dict(parts(msg))[choices[0]["part"]]
        target.set_param("filename", '<script>"unsafe"</script>.bin', header="Content-Disposition")
        choices[0]["drive_url"] += "?a=1&b=2"
        _, _, changed = transform(msg.as_bytes(), choices, "op")
        html = next(body_text(p) for _, p in parts(changed) if p.get_content_type() == "text/html")
        self.assertNotIn('<script>"unsafe"</script>', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("?a=1&amp;b=2", html)

    def test_invalid_or_credential_bearing_drive_urls_are_rejected(self):
        raw, choices = fixture()
        for url in ("http://drive.google.com/file/d/a", "https://evil.example/file/d/a",
                    "https://user:secret@drive.google.com/file/d/a", "https://drive.google.com/file/d/",
                    "https://drive.google.com:invalid/file/d/a",
                    "https://drive.google.com/file/d/a\nInjected: yes"):
            choices[0]["drive_url"] = url
            with self.subTest(url=url), self.assertRaises(CleanupError):
                transform(raw, choices, "op")

    def test_already_transformed_message_is_rejected(self):
        raw, choices = fixture()
        result, _, _ = transform(raw, choices, "op")
        with self.assertRaises(Unsupported):
            transform(result, choices, "secondop")
