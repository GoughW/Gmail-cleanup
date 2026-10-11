"""Deterministic synthetic fixture builder shared by offline and opt-in live tests."""

from email.message import EmailMessage
from email import policy
from .mime import digest, parts

TEST_MARKER = "X-Gmail-Cleanup-Test"
CASES = ("plain", "html", "received", "sent", "multiple", "inline", "unicode",
         "duplicate", "large", "thread-a", "thread-b")


def fixture(case="plain", run_id="offline", large_size=4 * 1024 * 1024):
    if case not in CASES:
        raise ValueError("Unknown synthetic case")
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "Synthetic sender <sender@example.invalid>"
    msg["To"] = "Synthetic recipient <recipient@example.invalid>"
    msg["Cc"] = "Synthetic copy <copy@example.invalid>"
    msg["Subject"] = f"GMAIL-CLEANUP-SYNTHETIC {run_id} " + ("thread" if case.startswith("thread-") else case)
    msg["Date"] = "Wed, 07 Oct 2026 12:34:56 +0000"
    msg["Message-ID"] = f"<gmail-cleanup-{run_id}-{case}@example.invalid>"
    msg[TEST_MARKER] = f"{run_id}:{case}"
    if case == "thread-b":
        msg["In-Reply-To"] = f"<gmail-cleanup-{run_id}-thread-a@example.invalid>"
        msg["References"] = f"<gmail-cleanup-{run_id}-thread-a@example.invalid>"
    msg.set_content("Synthetic correspondence only. Café, 東京, 🙂.\n")
    if case in {"html", "inline"}:
        msg.add_alternative('<html><body><p>Synthetic correspondence: Café, 東京, 🙂.</p>'
                            + ('<img src="cid:synthetic-image">' if case == "inline" else "")
                            + '</body></html>\n', subtype="html")
    if case == "inline":
        msg.get_payload()[1].add_related(b"synthetic-image-bytes", maintype="image", subtype="png",
                                        cid="<synthetic-image>", disposition="inline", filename="inline.png")
    data = (bytes(range(256)) * ((large_size + 255) // 256))[:large_size] if case == "large" else b"remove this synthetic attachment\x00\xff"
    filename = "文書-café-🙂.bin" if case == "unicode" else "remove.bin"
    if case == "duplicate":
        filename = "same.bin"
    msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    if case in {"multiple", "duplicate", "inline"}:
        msg.add_attachment(b"keep this distinct synthetic attachment", maintype="application",
                           subtype="octet-stream", filename="same.bin" if case == "duplicate" else "keep.bin")
    # Fixed boundaries make generation reproducible and restart-friendly.
    for path, part in parts(msg):
        if part.is_multipart():
            part.set_boundary(f"gmail-cleanup-{run_id}-{case}-{path}")
    raw = msg.as_bytes()
    path, target = next((p, leaf) for p, leaf in parts(msg)
                        if leaf.get_content_disposition() == "attachment")
    sha = digest(target.get_payload(decode=True))
    selections = [{"part": path, "decoded_sha256": sha, "retained_sha256": sha,
                   "drive_url": f"https://drive.google.com/file/d/SYNTHETIC-{run_id}-{case}/view"}]
    return raw, selections
