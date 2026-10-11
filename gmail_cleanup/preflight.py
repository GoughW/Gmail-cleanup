"""Back up explicitly selected attachments and reconstruct locally. Never inserts mail."""

import uuid

from .drive import Backups
from .engine import build_plan, check_replacement, plan_message, snapshot
from .errors import CleanupError
from .manifest import select
from .mime import digest, inventory, parse, parts, transport_header_changes

UNRESOLVED_URL = "https://drive.google.com/file/d/UNRESOLVED_BACKUP/view"


def run(api, store, manifest, drive=None, drive_error=None, folder_id=None, allow_upload=False,
        stop_on_failure=False):
    rows, resolved, before = [], {}, {}
    for message_id in manifest["allowlist"]:
        row = {"message_id": message_id, "subject": None, "attachments_found": [],
               "selected_backups": [], "local_reconstruction_verified": False,
               "ready_to_create": False, "replacement_created": False, "gmail_write_requests": 0,
               "gmail_insert_behaviour": "untested; preview performs no mailbox insert"}
        rows.append(row)
        try:
            choices = select(manifest, message_id)
            message = api.get(message_id)
            if message.get("id") != message_id:
                raise CleanupError("Source response has the wrong message ID")
            before[message_id] = snapshot(message)
            parsed = parse(message["raw_bytes"])
            row["subject"] = str(parsed.get("Subject", ""))
            row["attachments_found"] = [item for item in inventory(parsed)
                                        if item.get("disposition") == "attachment"]
            row["transport_headers_removed"] = transport_header_changes(parsed)
            indexed = dict(parts(parsed))
            selections = [{"part": c["part"], "decoded_sha256": c["decoded_sha256"],
                           "retained_sha256": c["decoded_sha256"],
                           # Local MIME validation placeholder only. The final
                           # selections always use verified Drive webViewLink.
                           "drive_url": UNRESOLVED_URL} for c in choices]
            existing = store.get(message_id)
            if existing and (existing["state"] != "planned" or existing.get("replacement_id")):
                raise CleanupError("Existing operation is pending/completed; inspect status or reconcile before previewing a new creation")
            operation_id = existing["plan"]["operation_id"] if existing else uuid.uuid4().hex
            row["operation_id"] = operation_id
            # Validate the whole transformation before any Drive upload is attempted.
            plan, raw = build_plan(message, selections, operation_id)
            for choice, selection in zip(choices, selections):
                part = indexed[choice["part"]]
                payload = part.get_payload(decode=True)
                backup = {"part": choice["part"], "filename": part.get_filename(),
                          "source_sha256": digest(payload), "drive_verified": False,
                          "intended_destination": folder_id or "Gmail attachment backups (app-created folder)",
                          "drive_url": None, "downloaded_sha256": None}
                row["selected_backups"].append(backup)
                try:
                    if drive is None:
                        raise CleanupError(drive_error or "Drive client is unavailable")
                    proof = Backups(drive, store).ensure(payload, part.get_filename(), folder_id,
                                                       choice.get("drive_file_id"), allow_upload)
                    backup.update(proof, drive_verified=True)
                    selection["drive_url"] = proof["drive_url"]
                except CleanupError as exc:
                    backup.update(getattr(exc, "observation", {}))
                    backup["error"] = str(exc)
                    if stop_on_failure:
                        raise CleanupError("Drive backup verification failed; stop before further processing") from None
            plan, raw = build_plan(message, selections, operation_id)
            # This checks only local MIME/header/label/date expectations, not Gmail's behaviour.
            local = {"id": "local-preview-" + operation_id, "raw_bytes": raw,
                     "labelIds": message.get("labelIds", []), "threadId": message["threadId"],
                     "internalDate": str(plan["expected"]["date_header_ms"])}
            verification = check_replacement(local, plan)
            row.update(local_verification=verification, local_reconstruction_verified=verification["passed"],
                       header_changes=plan["header_changes"],
                       predicted_ordering_delta_ms=plan["ordering_delta_ms_if_date_header_used"],
                       original_headers_sha256=plan["original_headers_sha256"],
                       reconstructed_headers_sha256=plan["expected"]["headers_sha256"],
                       replacement_size_bytes=len(raw), original_size_bytes=len(message["raw_bytes"]),
                       other_header_changes=["X-Gmail-Attachment-Cleanup added", "body MIME charset/transfer encoding normalized to UTF-8/base64"])
            row["original_unchanged"] = snapshot(api.get(message_id)) == before[message_id]
            row["ready_to_create"] = (verification["passed"] and row["original_unchanged"]
                                      and all(b["drive_verified"] for b in row["selected_backups"]))
            if row["ready_to_create"]:
                resolved[message_id] = selections
        except CleanupError as exc:
            row["error"] = str(exc)
            if stop_on_failure:
                break
    # Verify every inspected original again at the end of the batch.
    for row in rows:
        key = row["message_id"]
        if key in before:
            try:
                row["original_unchanged"] = snapshot(api.get(key)) == before[key]
            except CleanupError:
                row["original_unchanged"] = False
            row["ready_to_create"] &= row["original_unchanged"]
    output_manifest = None
    if len(rows) == len(manifest["allowlist"]) and all(row["ready_to_create"] for row in rows):
        output_manifest = {"version": 1, "allowlist": manifest["allowlist"], "messages": resolved}
        for row in rows:
            key = row["message_id"]
            try:
                record = plan_message(api, store, output_manifest, key,
                                      operation_id=row["operation_id"], expected_original=before[key])
                record["drive_expectations"] = row["selected_backups"]
                store.save(record, expected_state="planned")
                row["plan_sha256"] = record["plan"]["plan_sha256"]
            except CleanupError as exc:
                row.update(ready_to_create=False, error=str(exc))
        if not all(row["ready_to_create"] for row in rows):
            output_manifest = None
    return {"mode": "backup-and-local-reconstruction-preview", "gmail_write_requests": 0,
            "message_count": len(rows), "all_ready_to_create": len(rows) == len(manifest["allowlist"]) and all(row["ready_to_create"] for row in rows),
            "results": rows}, output_manifest
