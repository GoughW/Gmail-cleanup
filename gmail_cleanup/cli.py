import argparse
import json
import sqlite3

from .api import Gmail, OfflineMailbox, authenticate
from .engine import (create, plan_message, reconcile, require_record, snapshot,
                     trash_original, verify)
from .errors import CleanupError, Unsupported
from .manifest import load_manifest, load_backup_manifest, select
from .mime import date_ms, inventory, parse, validate_structure
from .storage import Store, write_private_json


def backend(args, writes=False):
    if args.offline_sources:
        if writes:
            raise CleanupError("Mailbox writes are unavailable in offline mode")
        return OfflineMailbox(args.offline_sources)
    if args.env_auth:
        return Gmail.from_environment(require_modify=writes)
    if not args.token:
        raise CleanupError("Specify an offline source inventory or an explicit user-owned OAuth token")
    return Gmail(args.token, require_modify=writes)


def summary(record, dry_run=False):
    result = {"message_id": record["message_id"], "state": record["state"], "dry_run": dry_run,
              "replacement_id": record["replacement_id"],
              "plan_sha256": record["plan"]["plan_sha256"],
              "removed_attachment_count": len(record["plan"]["removed"]),
              "original_size_bytes": record["plan"]["original_size_bytes"],
              "replacement_size_bytes": record["plan"]["expected"]["size_bytes"],
              "net_size_savings_bytes": record["plan"]["net_size_savings_bytes"],
              "predicted_ordering_delta_ms": record["plan"]["ordering_delta_ms_if_date_header_used"]}
    if record.get("verification"):
        result["verification"] = record["verification"]
    return result


def parser():
    root = argparse.ArgumentParser(description="Explicit Gmail MIME replacement; dry-run by default")
    commands = root.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Desktop OAuth, run on a machine with your browser")
    auth.add_argument("--credentials", required=True)
    auth.add_argument("--token", required=True)
    auth.add_argument("--modify", action="store_true", help="Request gmail.modify rather than read-only")
    auth.add_argument("--drive", action="store_true", help="Also request Drive drive.file for app-created backups")
    check = commands.add_parser("auth-check", help="Refresh OAuth and initialize the client; no mailbox requests")
    check_sources = check.add_mutually_exclusive_group(required=True)
    check_sources.add_argument("--token")
    check_sources.add_argument("--env-auth", action="store_true")
    check.set_defaults(offline_sources=None)
    preview = commands.add_parser("preflight", help="Verify/create Drive backups and reconstruct locally; never modifies Gmail")
    preview.add_argument("--manifest", required=True, help="Version-2 explicit MIME part/hash allowlist")
    preview.add_argument("--output", required=True, help="Private per-message report")
    preview.add_argument("--resolved-manifest", help="Write a version-1 creation manifest only if every message passes")
    preview.add_argument("--state", default=".private/audit.sqlite")
    preview.add_argument("--drive-folder-id")
    preview.add_argument("--upload-backups", action="store_true", help="Allow new Drive files/folder; Gmail remains read-only")
    preview_sources = preview.add_mutually_exclusive_group(required=True)
    preview_sources.add_argument("--token")
    preview_sources.add_argument("--env-auth", action="store_true")
    preview.set_defaults(offline_sources=None, message_id=None)
    workflow = commands.add_parser("workflow", help="Sequential disposable-account test; stops on first failure")
    workflow.add_argument("--manifest", required=True, help="Version-2 exact ID/part/hash allowlist")
    workflow.add_argument("--state", default=".private/audit.sqlite")
    workflow.add_argument("--output", required=True)
    workflow.add_argument("--execute", action="store_true")
    workflow.add_argument("--enable-trash", action="store_true")
    workflow.add_argument("--disposable-test-account", action="store_true")
    workflow.add_argument("--upload-backups", action="store_true")
    workflow.add_argument("--drive-folder-id")
    workflow_auth = workflow.add_mutually_exclusive_group(required=True)
    workflow_auth.add_argument("--token")
    workflow_auth.add_argument("--env-auth", action="store_true")
    workflow.set_defaults(offline_sources=None, message_id=None)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", required=True)
    common.add_argument("--message-id", required=True, help="One explicit allowlisted Gmail message ID")
    common.add_argument("--state", default=".private/audit.sqlite")
    sources = common.add_mutually_exclusive_group()
    sources.add_argument("--token")
    sources.add_argument("--offline-sources")
    sources.add_argument("--env-auth", action="store_true", help="Use the named Gmail OAuth environment bindings")
    inspect = commands.add_parser("inspect", parents=[common], help="Read-only private MIME inventory")
    inspect.add_argument("--output", required=True)
    plan = commands.add_parser("plan", parents=[common], help="Read-only mailbox inspection and exact plan")
    plan.add_argument("--output", required=True, help="Write full plan to a private local JSON file")
    create_parser = commands.add_parser("create", parents=[common], help="Insert and verify a replacement only")
    create_parser.add_argument("--execute", action="store_true")
    commands.add_parser("verify", parents=[common], help="Fetch and reverify a recorded replacement")
    recovery = commands.add_parser("reconcile", parents=[common], help="Inspect/recover a lost insert response")
    recovery.add_argument("--replacement-id", required=True)
    recovery.add_argument("--execute", action="store_true", help="Persist recovery; this only reads the mailbox")
    trash = commands.add_parser("trash", parents=[common], help="Separately enabled, approved later-phase trash")
    trash.add_argument("--execute", action="store_true")
    trash.add_argument("--enable-trash", action="store_true")
    trash.add_argument("--approve-plan")
    trash.add_argument("--approve-replacement")
    commands.add_parser("status", parents=[common], help="Inspect durable state without mailbox access")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        if args.command == "auth":
            authenticate(args.credentials, args.token, modify=args.modify, drive=args.drive)
            print(json.dumps({"authenticated": True, "write_scope": args.modify, "drive_scope": args.drive}))
            return 0
        if args.command == "workflow":
            from .workflow import run, empty_report
            manifest = load_backup_manifest(args.manifest)
            try:
                if args.execute and not (args.enable_trash and args.disposable_test_account):
                    raise CleanupError("Execution requires --enable-trash and --disposable-test-account")
                api = backend(args, writes=args.execute)
            except CleanupError as exc:
                report = empty_report(manifest, args.execute)
                report.update(error=str(exc), stopped_at=manifest["allowlist"][0])
                report["results"][0].update(state="blocked_setup", error=str(exc))
                write_private_json(args.output, report)
                print(json.dumps({"completed": False, "stopped_at": report["stopped_at"], "error": str(exc)}))
                return 2
            store = Store(args.state)
            report = run(api, store, manifest, args.output, args.execute, args.enable_trash,
                         args.disposable_test_account, args.upload_backups, args.drive_folder_id)
            print(json.dumps({"completed": report["completed"], "stopped_at": report["stopped_at"],
                              "trashed_original_count": sum(r["original_trashed"] for r in report["results"])}))
            return 0 if report["completed"] else 2
        if args.command == "preflight":
            from .preflight import run
            manifest = load_backup_manifest(args.manifest)
            api = backend(args, writes=False)
            store = Store(args.state)
            drive, error = None, None
            try:
                drive = api.drive
            except CleanupError as exc:
                error = str(exc)
            report, resolved = run(api, store, manifest, drive, error, args.drive_folder_id, args.upload_backups)
            write_private_json(args.output, report)
            if resolved and args.resolved_manifest:
                write_private_json(args.resolved_manifest, resolved)
            print(json.dumps({"message_count": report["message_count"],
                              "all_ready_to_create": report["all_ready_to_create"],
                              "gmail_write_requests": 0,
                              "resolved_manifest_written": bool(resolved and args.resolved_manifest)}))
            return 0 if report["all_ready_to_create"] else 2
        if args.command == "auth-check":
            backend(args)
            print(json.dumps({"oauth_ready": True, "mailbox_requests": 0,
                              "credential_source": "environment" if args.env_auth else "file"}))
            return 0
        manifest = load_manifest(args.manifest, allow_empty=args.command == "inspect")
        select(manifest, args.message_id)
        if args.command == "inspect":
            message = backend(args).get(args.message_id)
            if message.get("id") != args.message_id:
                raise CleanupError("Source response has the wrong message ID")
            parsed = parse(message["raw_bytes"])
            reason = None
            try:
                validate_structure(parsed)
                metadata = snapshot(message)
                metadata["date_header_ms"] = date_ms(parsed)
            except Unsupported as exc:
                reason = str(exc)
                metadata = {"id": args.message_id}
            write_private_json(args.output, {"original": metadata, "parts": inventory(parsed),
                                            "unsupported_reason": reason})
            print(json.dumps({"message_id": args.message_id, "inspected": True,
                              "unsupported_reason": reason}))
            return 0
        store = Store(args.state)
        if args.command == "plan":
            record = plan_message(backend(args), store, manifest, args.message_id)
            write_private_json(args.output, record["plan"])
        elif args.command == "create":
            record = create(backend(args, writes=True) if args.execute else None,
                            store, manifest, args.message_id, execute=args.execute)
        elif args.command == "verify":
            record = require_record(store, manifest, args.message_id)
            if record["state"] not in {"inserted", "verify_failed", "verified"}:
                raise CleanupError("Only recorded, untrashed replacements may be reverified")
            record = verify(backend(args), store, record)
        elif args.command == "reconcile":
            record = reconcile(backend(args), store, manifest, args.message_id,
                               args.replacement_id, execute=args.execute)
            if not args.execute:
                print(json.dumps({"dry_run": True, "candidate_verification": record}, sort_keys=True))
                return 0 if record["passed"] else 2
        elif args.command == "trash":
            # Check gates before initializing authentication or making any request.
            if args.execute and not (args.enable_trash and args.approve_plan and args.approve_replacement):
                raise CleanupError("Trash requires --enable-trash and exact plan/replacement approval")
            record = trash_original(backend(args, writes=True) if args.execute else None,
                                    store, manifest, args.message_id, execute=args.execute,
                                    enable_trash=args.enable_trash, approve_plan=args.approve_plan,
                                    approve_replacement=args.approve_replacement)
        else:
            record = require_record(store, manifest, args.message_id)
        print(json.dumps(summary(record, dry_run=args.command in {"create", "trash"} and not args.execute),
                         sort_keys=True))
        return 2 if record["state"] in {"verify_failed", "trashed_verify_failed"} else 0
    except (CleanupError, OSError, sqlite3.Error) as exc:
        # Third-party exception strings may contain raw requests or OAuth values.
        safe = str(exc) if isinstance(exc, CleanupError) else "Local private-file/state operation failed"
        result = {"error": safe}
        if store is not None:
            try:
                saved = store.get(args.message_id)
                if saved:
                    result.update(state=saved["state"], replacement_id=saved["replacement_id"])
            except sqlite3.Error:
                pass
        print(json.dumps(result, sort_keys=True))
        return 2
    finally:
        if store is not None:
            store.close()
