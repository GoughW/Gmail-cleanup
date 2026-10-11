"""Explicit disposable-account batch: finish one message before starting the next."""

from .drive import file_id_from_url
from .engine import (correspondence_observation, create, trash_original,
                     verify, verify_after_trash, snapshot)
from .errors import CleanupError
from .mime import parse
from .preflight import run as preflight
from .storage import write_private_json


def empty_report(manifest, execute):
    return {"mode": "sequential-disposable-test", "final_action": "trash", "execute": execute, "completed": False,
            "stopped_at": None, "results": [
                {"original_id": key, "replacement_id": None, "state": "not_processed",
                 "original": None, "replacement": None, "drive_backups": [],
                 "verification": None, "original_trashed": False,
                 "gmail_ui_observed": False}
                for key in manifest["allowlist"]]}


def same_source_selections(saved, choices):
    return (len(saved) == len(choices) and all(
        actual["part"] == wanted["part"] and actual["decoded_sha256"] == wanted["decoded_sha256"]
        and (not wanted.get("drive_file_id")
             or file_id_from_url(actual["drive_url"]) == wanted["drive_file_id"])
        for actual, wanted in zip(saved, choices)))


def drive_locations(drive, proofs):
    result = []
    for proof in proofs:
        item = dict(proof)
        try:
            metadata = drive.get(proof["file_id"])
            item["file_name"] = metadata.get("name") if metadata else None
            folders = []
            for key in proof.get("parents", []):
                parent = drive.get(key)
                folders.append({"id": key, "name": parent.get("name") if parent else None})
            item["folders"] = folders
            if item["file_name"] and len(folders) == 1 and folders[0]["name"]:
                item["path"] = folders[0]["name"] + "/" + item["file_name"]
            else:
                item["path"] = None
            item["path_scope"] = "immediate parent/name; complete ancestor hierarchy not asserted"
        except CleanupError:
            item["path"] = None
            item["path_error"] = "Folder/name metadata could not be fetched; file ID/hash remain recorded"
        result.append(item)
    return result


def update_from_record(row, record):
    row.update(state=record["state"], replacement_id=record.get("replacement_id"),
               verification=record.get("verification"),
               post_trash_verification=record.get("post_trash_verification"),
               original_trashed=record["state"] in {"trashed", "trashed_verify_failed"})
    if record.get("workflow_audit"):
        row.update(record["workflow_audit"])
    audit = record.get("trash_audit", {})
    if audit:
        row["original"] = audit.get("original", row["original"])
        row["replacement"] = audit.get("replacement_after_trash", audit.get("replacement_before_trash", row["replacement"]))
        row["original_after_trash"] = audit.get("original_after_trash")
    if row.get("original") and row.get("replacement"):
        before, after = row["original"], row["replacement"]
        row["header_preservation"] = {name: before["headers"][name] == after["headers"][name]
                                      for name in before["headers"]}
        row["observed_differences"] = {
            "gmail_id_changed": before["id"] != after["id"],
            "thread_changed": before["thread_id"] != after["thread_id"],
            "labels_changed": before["labels"] != after["labels"],
            "internal_date_delta_ms": after["internal_date_ms"] - before["internal_date_ms"],
            "raw_size_delta_bytes": after["size_bytes"] - before["size_bytes"],
            "note": "API observations only; attachment removal, retained-file notes and audited header edits are intentional"}


def run(api, store, manifest, output, execute=False, enable_trash=False,
        disposable_test_account=False, allow_upload=False, folder_id=None):
    report = empty_report(manifest, execute)
    write_private_json(output, report)  # output must be writable before any mutation
    try:
        if execute and not (enable_trash and disposable_test_account):
            raise CleanupError("Execution requires --enable-trash and --disposable-test-account")
        if execute and not getattr(api, "can_modify", False):
            raise CleanupError("Sequential workflow requires explicitly enabled gmail.modify capability")
        drive = api.drive
    except CleanupError as exc:
        report.update(error=str(exc), stopped_at=manifest["allowlist"][0])
        report["results"][0].update(state="blocked_setup", error=str(exc))
        write_private_json(output, report)
        return report
    for row in report["results"]:
        key = row["original_id"]
        resolved = None
        try:
            existing = store.get(key)
            if existing and not same_source_selections(existing["plan"]["selections"], manifest["messages"][key]):
                raise CleanupError("Saved operation does not match the exact source selections")
            if existing and existing["state"] in {"trash_pending", "trashed", "trashed_verify_failed"}:
                if not execute:
                    update_from_record(row, existing)
                    continue
                record = verify_after_trash(api, store, existing)
                update_from_record(row, record)
                if record["state"] != "trashed":
                    raise CleanupError("Post-Trash verification failed; stop before processing the next message")
                write_private_json(output, report)
                continue
            if existing and existing["state"] in {"insert_pending", "verify_failed"}:
                update_from_record(row, existing)
                raise CleanupError("Incomplete or failed prior operation requires explicit inspection/reconciliation")
            original = api.get(key)
            if original.get("id") != key:
                raise CleanupError("Source response has the wrong original ID")
            row.update(original=correspondence_observation(original), subject=str(parse(original["raw_bytes"])["Subject"]))
            write_private_json(output, report)
            if existing and existing["state"] in {"inserted", "verified"}:
                resolved = {"version": 1, "allowlist": [key], "messages": {key: existing["plan"]["selections"]}}
                if not execute:
                    update_from_record(row, existing)
                    continue
                record = verify(api, store, existing)
            else:
                single = {"version": 2, "allowlist": [key], "messages": {key: manifest["messages"][key]}}
                preview, resolved = preflight(api, store, single, drive, folder_id=folder_id,
                                               allow_upload=allow_upload if execute else False,
                                               stop_on_failure=True)
                row["preflight"] = preview["results"][0]
                row["drive_backups"] = preview["results"][0]["selected_backups"]
                if not preview["all_ready_to_create"]:
                    raise CleanupError("Drive backup or local reconstruction verification failed; original remains untouched")
                row["state"] = "planned"
                if store.get(key)["plan"]["original"] != snapshot(original):
                    raise CleanupError("Original changed during workflow preparation; stop before insertion")
                write_private_json(output, report)
                if not execute:
                    continue
                record = create(api, store, resolved, key, execute=True)
            update_from_record(row, record)
            if record.get("replacement_id"):
                row["replacement"] = correspondence_observation(api.get(record["replacement_id"]))
                update_from_record(row, record)
            if record["state"] != "verified" or not record["verification"]["passed"]:
                raise CleanupError("Replacement verification failed; original remains untouched")
            row["drive_backups"] = drive_locations(drive, record["drive_verification"])
            row["proposed_header_changes"] = record["plan"]["header_changes"]
            # Persist full requested audit metadata before the Trash request.
            record["workflow_audit"] = {name: row[name] for name in (
                "original", "replacement", "drive_backups", "proposed_header_changes", "subject")}
            store.save(record, expected_state="verified")
            write_private_json(output, report)
            record = trash_original(api, store, resolved, key, execute=True,
                enable_trash=True, approve_plan=record["plan"]["plan_sha256"],
                approve_replacement=record["replacement_id"])
            update_from_record(row, record)
            write_private_json(output, report)
            if record["state"] != "trashed":
                raise CleanupError("Post-Trash verification failed; stop before processing the next message")
        except CleanupError as exc:
            saved = store.get(key)
            if saved:
                update_from_record(row, saved)
            row["error"] = str(exc)
            if row["state"] in {"not_processed", "planned"}:
                row["state"] = "stopped_before_insert"
            report.update(stopped_at=key, error=str(exc))
            write_private_json(output, report)
            return report
    report["completed"] = True
    write_private_json(output, report)
    return report
