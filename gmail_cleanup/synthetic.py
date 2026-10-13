"""Live harness: only replacements of registered, uniquely marked dummy originals.

This module has no trash path or sending. Its preflight can create Drive backups.
Original synthetic messages must be inserted by the user before registration.
"""

import argparse
import json
import os
from pathlib import Path
import uuid

from .api import Gmail
from .engine import create, plan_message
from .errors import CleanupError
from .fixtures import CASES, TEST_MARKER, fixture
from .manifest import load_manifest
from .mime import digest, json_digest, parse
from .storage import Store, write_private_json
from .paths import ACTIVE, cli_paths


def generate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    run_id = uuid.uuid4().hex
    cases = {}
    sources = {}
    selections = {}
    for case in CASES:
        raw, selected = fixture(case, run_id)
        location = directory / f"{case}.eml"
        fd = os.open(location, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
        cases[case] = {"raw_file": location.name, "raw_sha256": digest(raw), "selections": selected}
        # Convenient local-only walkthrough; IDs are deliberately not Gmail IDs.
        key = "offline" + case.replace("-", "")
        sources[key] = {"raw_file": location.name, "threadId": "offlinethread" if case.startswith("thread-") else key,
                        "internalDate": "1791376496000", "labelIds": ["SENT"] if case == "sent" else ["INBOX", "UNREAD", "STARRED"]}
        selections[key] = selected
    write_private_json(directory / "fixtures.json", {"version": 1, "run_id": run_id, "cases": cases}, exclusive=True)
    write_private_json(directory / "offline-sources.json", sources, exclusive=True)
    write_private_json(directory / "offline-selections.json", {"version": 1, "allowlist": list(selections), "messages": selections}, exclusive=True)
    return {"generated_fixture_count": len(cases), "mailbox_access": False}


def assert_synthetic(message, run_id, case, raw_sha):
    if case not in CASES or not run_id.isalnum() or not run_id:
        raise CleanupError("Invalid synthetic fixture registry")
    msg = parse(message["raw_bytes"])
    if (msg.get_all(TEST_MARKER) != [f"{run_id}:{case}"]
            or str(msg.get("Subject")) != f"GMAIL-CLEANUP-SYNTHETIC {run_id} " + ("thread" if case.startswith("thread-") else case)
            or msg.get_all("Message-ID") != [f"<gmail-cleanup-{run_id}-{case}@example.invalid>"]
            or digest(message["raw_bytes"]) != raw_sha):
        raise CleanupError("Synthetic original marker/identity/hash mismatch; no write permitted")
    expected_labels = {"SENT", "STARRED"} if case == "sent" else {"INBOX", "UNREAD", "STARRED"}
    if set(message.get("labelIds", [])) != expected_labels:
        raise CleanupError("Synthetic fixture does not have the required sent/received/read/starred label state")


def register(api, directory, ids, output):
    directory = Path(directory)
    index = json.loads((directory / "fixtures.json").read_text())
    if (set(ids) != set(CASES) or len(set(ids.values())) != len(ids)
            or any(not isinstance(i, str) or not i.isalnum() for i in ids.values())):
        raise CleanupError("Supply exactly one unique explicit Gmail ID per generated synthetic case")
    manifest = {"version": 1, "allowlist": list(ids.values()), "messages": {},
                "synthetic": {"run_id": index["run_id"], "originals": {}}}
    for case, message_id in ids.items():
        expected = index["cases"][case]
        # Verify registry still matches the local fixture, not only a user-supplied hash.
        raw = (directory / expected["raw_file"]).read_bytes()
        if digest(raw) != expected["raw_sha256"]:
            raise CleanupError("Local synthetic fixture changed")
        message = api.get(message_id)
        if message.get("id") != message_id:
            raise CleanupError("Synthetic response identity mismatch")
        assert_synthetic(message, index["run_id"], case, expected["raw_sha256"])
        manifest["messages"][message_id] = expected["selections"]
        manifest["synthetic"]["originals"][message_id] = {"case": case, "raw_sha256": expected["raw_sha256"]}
    if api.get(ids["thread-a"])["threadId"] != api.get(ids["thread-b"])["threadId"]:
        raise CleanupError("The two synthetic thread originals must belong to the same Gmail thread")
    write_private_json(output, manifest, exclusive=True)
    return {"registered_synthetic_original_count": len(ids), "mailbox_writes": 0}


class SyntheticOnly:
    """Rechecks the exact recorded original immediately before each insert."""
    def __init__(self, api, manifest, message_id):
        self.api, self.manifest, self.message_id = api, manifest, message_id

    @property
    def drive(self):
        return self.api.drive

    def get(self, message_id):
        message = self.api.get(message_id)
        registry = self.manifest["synthetic"]
        if message_id in registry["originals"]:
            entry = registry["originals"][message_id]
            assert_synthetic(message, registry["run_id"], entry["case"], entry["raw_sha256"])
        return message

    def get_thread(self, thread_id):
        return self.api.get_thread(thread_id)

    def insert(self, raw, labels, thread_id):
        registry = self.manifest["synthetic"]
        entry = registry["originals"][self.message_id]
        original = self.api.get(self.message_id)
        if original.get("id") != self.message_id:
            raise CleanupError("Synthetic source response identity mismatch")
        assert_synthetic(original, registry["run_id"], entry["case"], entry["raw_sha256"])
        return self.api.insert(raw, labels, thread_id)

    def mailbox_identity(self):
        return self.api.mailbox_identity()


def run(api, store, manifest, execute=False):
    registry = manifest.get("synthetic", {})
    originals = registry.get("originals", {})
    if (set(originals) != set(manifest["allowlist"])
            or {item["case"] for item in originals.values()} != set(CASES)):
        raise CleanupError("An intact complete synthetic registration manifest is required")
    # Validate the entire allowlist before any insert, then recheck each just before use.
    before = {}
    for message_id, entry in originals.items():
        message = api.get(message_id)
        if message.get("id") != message_id:
            raise CleanupError("Synthetic source response identity mismatch")
        assert_synthetic(message, registry["run_id"], entry["case"], entry["raw_sha256"])
        before[message_id] = json_digest({"labels": sorted(message.get("labelIds", [])),
                                        "date": message.get("internalDate"), "thread": message.get("threadId"),
                                        "raw_sha256": digest(message["raw_bytes"])})
    results = []
    for message_id in manifest["allowlist"]:
        guarded = SyntheticOnly(api, manifest, message_id)
        record = plan_message(guarded, store, manifest, message_id)
        record = create(guarded, store, manifest, message_id, execute=execute)
        after = api.get(message_id)
        after_digest = json_digest({"labels": sorted(after.get("labelIds", [])),
                                    "date": after.get("internalDate"), "thread": after.get("threadId"),
                                    "raw_sha256": digest(after["raw_bytes"])})
        results.append({"message_id": message_id, "case": originals[message_id]["case"],
                        "state": record["state"], "replacement_id": record["replacement_id"],
                        "original_unchanged": before[message_id] == after_digest,
                        "verification": record["verification"]})
        if before[message_id] != after_digest:
            raise CleanupError("Synthetic original metadata changed; stop and inspect")
    return {"dry_run": not execute, "results": results}


def prepare_backups(api, store, manifest, allow_upload=False, folder_id=None):
    from .preflight import run as preview
    registry = manifest.get("synthetic", {})
    originals = registry.get("originals", {})
    if (set(originals) != set(manifest["allowlist"])
            or {item["case"] for item in originals.values()} != set(CASES)):
        raise CleanupError("An intact complete synthetic registration manifest is required")
    guard = SyntheticOnly(api, manifest, None)
    for key in manifest["allowlist"]:
        guard.get(key)  # validate the complete fixture allowlist before any Drive creation
    selections = {key: [{"part": c["part"], "decoded_sha256": c["decoded_sha256"]}
                        for c in manifest["messages"][key]] for key in manifest["allowlist"]}
    drive, error = None, None
    try:
        drive = api.drive
    except CleanupError as exc:
        error = str(exc)
    report, resolved = preview(guard, store, {"version": 2, "allowlist": manifest["allowlist"],
                                            "messages": selections}, drive, error, folder_id, allow_upload)
    if resolved:
        resolved["synthetic"] = registry
    return report, resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description="Opt-in replacement-only synthetic Gmail harness")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--directory", required=True)
    reg = sub.add_parser("register", help="Read-only registration of user-imported synthetic originals")
    reg.add_argument("--directory", required=True)
    reg.add_argument("--ids", required=True, help="JSON mapping generated case names to explicit Gmail IDs")
    reg_auth = reg.add_mutually_exclusive_group(required=True)
    reg_auth.add_argument("--token")
    reg_auth.add_argument("--env-auth", action="store_true")
    reg.add_argument("--output", required=True)
    runner = sub.add_parser("run")
    runner.add_argument("--manifest", required=True)
    runner.add_argument("--state", required=True)
    runner.add_argument('--initialize-state', action='store_true')
    runner.add_argument('--mailbox-key')
    runner.add_argument('--journal')
    run_auth = runner.add_mutually_exclusive_group(required=True)
    run_auth.add_argument("--token")
    run_auth.add_argument("--env-auth", action="store_true")
    runner.add_argument("--execute", action="store_true")
    preflight = sub.add_parser("preflight", help="Verify Drive backups and reconstruct registered fixtures; no Gmail writes")
    preflight.add_argument("--manifest", required=True)
    preflight.add_argument("--state", required=True)
    preflight.add_argument('--initialize-state', action='store_true')
    preflight.add_argument('--mailbox-key')
    preflight.add_argument('--journal')
    preview_auth = preflight.add_mutually_exclusive_group(required=True)
    preview_auth.add_argument("--token")
    preview_auth.add_argument("--env-auth", action="store_true")
    preflight.add_argument("--output", required=True)
    preflight.add_argument("--resolved-manifest", required=True)
    preflight.add_argument("--upload-backups", action="store_true")
    preflight.add_argument("--drive-folder-id")
    store = None
    paths, path_token = None, None
    try:
        if args := parser.parse_args(argv):
            paths = cli_paths(args)
            path_token = ACTIVE.set(paths)
            if args.command == "generate":
                result = generate(args.directory)
            elif args.command == "register":
                api = Gmail.from_environment() if args.env_auth else Gmail(args.token)
                result = register(api, args.directory,
                                  json.loads(Path(args.ids).read_text()), args.output)
            elif args.command == "preflight":
                manifest = load_manifest(args.manifest)
                api = Gmail.from_environment() if args.env_auth else Gmail(args.token)
                store = Store(args.state, initialize=args.initialize_state, mailbox_key=args.mailbox_key, journal=args.journal)
                report, resolved = prepare_backups(api, store, manifest, args.upload_backups, args.drive_folder_id)
                write_private_json(args.output, report)
                if resolved:
                    write_private_json(args.resolved_manifest, resolved)
                result = {"all_ready_to_create": report["all_ready_to_create"], "gmail_write_requests": 0,
                          "message_count": report["message_count"]}
                print(json.dumps(result, sort_keys=True))
                return 0 if report["all_ready_to_create"] else 2
            else:
                manifest = load_manifest(args.manifest)
                api = Gmail.from_environment(require_modify=args.execute) if args.env_auth else Gmail(args.token, require_modify=args.execute)
                store = Store(args.state, initialize=args.initialize_state, mailbox_key=args.mailbox_key, journal=args.journal)
                result = run(api, store, manifest, execute=args.execute)
            print(json.dumps(result, sort_keys=True))
            return 2 if any(r["state"] == "verify_failed" for r in result.get("results", [])) else 0
    except (CleanupError, OSError, ValueError, KeyError, TypeError):
        # Inspect the private audit for known IDs after an interruption.
        print(json.dumps({"error": "Synthetic harness stopped; inspect private state before retrying"}))
        return 2
    finally:
        if store:
            store.close()
        if path_token is not None:
            ACTIVE.reset(path_token)
        if paths is not None:
            paths.close()


if __name__ == "__main__":
    raise SystemExit(main())
