"""Fail-closed operation lifecycle; durable intent precedes every mailbox mutation."""

from copy import deepcopy
import uuid

from .errors import CleanupError, Unsupported
from .manifest import select
from .drive import verify_selections
from .mime import (MARKER, date_ms, digest, header_fingerprint, inventory, json_digest,
                   parse, transform, transport_header_changes, changed_headers,
                   semantic_root, all_header_differences)

SAFETY_POLICY = 2
TRANSFORMATION_POLICY = 2


def current_policy(plan):
    return plan.get('safety_policy') == SAFETY_POLICY and plan.get('transformation_policy') == TRANSFORMATION_POLICY


def mutation_binding(record):
    plan = record['plan']
    if record['message_id'] != plan['message_id'] or record['message_id'] != plan['original']['id']:
        raise CleanupError('Operation original identity conflicts')
    return {'original_id': record['message_id'], 'operation_id': plan['operation_id'],
            'plan_sha256': plan['plan_sha256']}


def snapshot(message):
    try:
        if not message.get("threadId") or message.get("classificationLabelValues"):
            raise Unsupported("Missing thread identity or unsupported classification labels")
        labels = sorted(message.get("labelIds", []))
        if any(label in labels for label in ("DRAFT", "DRAFTS", "TRASH", "SPAM")):
            raise Unsupported("Draft, spam and trashed originals are unsupported")
        return {"id": message["id"], "thread_id": message["threadId"],
                "labels": labels, "internal_date_ms": int(message["internalDate"]),
                "raw_sha256": digest(message["raw_bytes"])}
    except (KeyError, ValueError, TypeError):
        raise CleanupError("Incomplete or invalid Gmail metadata") from None


def build_plan(message, selections, operation_id):
    original = snapshot(message)
    raw, removed, modified = transform(message["raw_bytes"], selections, operation_id)
    parsed_original = parse(message["raw_bytes"])
    plan = {"version": 1, "safety_policy": SAFETY_POLICY, "transformation_policy": TRANSFORMATION_POLICY,
            "message_id": original["id"], "operation_id": operation_id,
            "original": original, "removed": removed, "selections": deepcopy(selections),
            "original_inventory": inventory(parsed_original),
            "original_headers_sha256": header_fingerprint(parsed_original, exclude_mime=True),
            "transport_headers_removed": transport_header_changes(parsed_original),
            "header_changes": changed_headers(parsed_original, modified),
            "body_note_format": "retained-manifest-v1; append text/html note; UTF-8/base64 bodies",
            "insert": {"method": "users.messages.insert", "internalDateSource": "dateHeader",
                       "thread_id": original["thread_id"], "labels": original["labels"]},
            "expected": {"raw_sha256": digest(raw), "inventory": inventory(modified),
                         "headers_sha256": header_fingerprint(modified, exclude_mime=True),
                         "date_header_ms": date_ms(modified), "size_bytes": len(raw)},
            "original_size_bytes": len(message["raw_bytes"]),
            "net_size_savings_bytes": len(message["raw_bytes"]) - len(raw),
            "ordering_delta_ms_if_date_header_used": date_ms(modified) - original["internal_date_ms"]}
    plan["plan_sha256"] = json_digest(plan)
    return plan, raw


def plan_message(api, store, manifest, message_id, operation_id=None, expected_original=None):
    selections = select(manifest, message_id)
    existing = store.get(message_id)
    if existing and not current_policy(existing['plan']):
        return existing  # Historical inspection only; never rewrite an approved plan.
    store.require_current(api)
    operation_id = existing["plan"]["operation_id"] if existing else (operation_id or uuid.uuid4().hex)
    message = api.get(message_id)
    if message.get("id") != message_id:
        raise CleanupError("Source response has the wrong message ID")
    plan, _ = build_plan(message, selections, operation_id)
    if expected_original is not None and plan["original"] != expected_original:
        raise CleanupError("Original changed during preflight; no creation manifest is ready")
    if existing:
        if existing["plan"]["plan_sha256"] != plan["plan_sha256"]:
            raise CleanupError("Original or selections changed; existing operation requires inspection")
        return existing
    record = {"message_id": message_id, "state": "planned", "plan": plan,
              "replacement_id": None, "verification": None}
    store.save(record, new=True)
    return record


def require_record(store, manifest, message_id):
    selections = select(manifest, message_id)
    record = store.get(message_id)
    if not record or record["plan"]["selections"] != selections:
        raise CleanupError("A matching persisted plan is required")
    plan = deepcopy(record["plan"])
    claimed = plan.pop("plan_sha256")
    if json_digest(plan) != claimed:
        raise CleanupError("Saved plan digest does not match its contents")
    return record


def unchanged_original(api, record):
    original = api.get(record["message_id"])
    if snapshot(original) != record["plan"]["original"]:
        raise CleanupError("Original content or metadata changed since planning")
    return original


def approved_reconstruction(original, plan):
    """Regenerate the immutable approved bytes, including for legacy saved plans."""
    raw, _, _ = transform(original["raw_bytes"], plan["selections"], plan["operation_id"])
    if digest(raw) != plan["expected"]["raw_sha256"]:
        raise CleanupError("Regenerated replacement differs from approved bytes")
    return raw


def check_replacement(message, plan, expected_raw=None, original_raw=None):
    """Compare approved content exactly, except narrowly listed root transport headers."""
    expected = plan["expected"]
    checks = {}
    differences = {}
    try:
        raw = message["raw_bytes"]
        parsed = parse(raw)
        expected_inventory = expected["inventory"]
        expected_headers = expected["headers_sha256"]
        comparison = parsed
        if expected_raw is not None:
            approved = parse(expected_raw)
            # A reference cannot replace or weaken the persisted approved plan.
            if (digest(expected_raw) != expected["raw_sha256"]
                    or inventory(approved) != expected_inventory
                    or header_fingerprint(approved, exclude_mime=True) != expected_headers):
                raise CleanupError("Expected reference does not match approved plan")
            comparison = semantic_root(parsed)
            semantic_approved = semantic_root(approved)
            expected_inventory = inventory(semantic_approved)
            expected_headers = header_fingerprint(semantic_approved, exclude_mime=True)
            differences["expected_to_observed_header_changes"] = all_header_differences(approved, parsed)
        if original_raw is not None:
            differences["original_to_replacement_header_changes"] = all_header_differences(parse(original_raw), parsed)
        checks["distinct_message_id"] = bool(message.get("id")) and message["id"] != plan["message_id"]
        checks["operation_marker"] = parsed.get_all(MARKER) == [plan["operation_id"]]
        checks["mime_contents_and_attachment_hashes"] = inventory(comparison) == expected_inventory
        checks["correspondence_and_threading_headers"] = header_fingerprint(comparison, exclude_mime=True) == expected_headers
        checks["labels_read_unread_starred_sent"] = sorted(message.get("labelIds", [])) == plan["original"]["labels"]
        checks["thread_membership"] = message.get("threadId") == plan["original"]["thread_id"]
        actual_date = int(message["internalDate"])
        checks["date_header"] = date_ms(parsed) == expected["date_header_ms"]
        checks["internal_date_source"] = actual_date == expected["date_header_ms"]
        checks["original_ordering_timestamp"] = actual_date == plan["original"]["internal_date_ms"]
        checks["classification_labels"] = not message.get("classificationLabelValues")
        differences.update(ordering_delta_ms=actual_date - plan["original"]["internal_date_ms"],
                           raw_bytes_differ=digest(raw) != expected["raw_sha256"])
        observed = {"id": message.get("id"), "raw_sha256": digest(raw),
                    "internal_date_ms": actual_date, "thread_id": message.get("threadId"),
                    "labels": sorted(message.get("labelIds", []))}
    except (CleanupError, KeyError, ValueError, TypeError):
        checks["parseable_complete_replacement"] = False
        observed = {"id": message.get("id")}
    return {"passed": bool(checks) and all(checks.values()), "checks": checks,
            "differences": differences, "observed": observed}


def verify(api, store, record):
    if record["state"] in {"trash_pending", "trashed", "trashed_verify_failed"}:
        raise CleanupError("Trash lifecycle has started; use read-only Trash recovery/status")
    if not record.get("replacement_id"):
        raise CleanupError("No recorded replacement ID; reconcile the incomplete insert first")
    current_state = record["state"]
    if not current_policy(record['plan']) or store.recovery_error:
        result = check_replacement(api.get(record['replacement_id']), record['plan'])
        result.update(current_policy_eligible=False, historical_only=True)
        observed = deepcopy(record)
        observed.update(state='recovery_only', verification=result)
        return observed
    try:
        original = unchanged_original(api, record)
        expected_raw = approved_reconstruction(original, record["plan"])
        message = api.get(record["replacement_id"])
        result = check_replacement(message, record["plan"], expected_raw, original["raw_bytes"])
        if message.get("id") != record["replacement_id"]:
            result["checks"]["recorded_replacement_id"] = False
            result["passed"] = False
        try:
            thread = api.get_thread(record["plan"]["original"]["thread_id"])
            members = {item["id"] for item in thread.get("messages", [])}
            result["checks"]["thread_contains_original_and_replacement"] = (
                thread.get("id") == record["plan"]["original"]["thread_id"]
                and {record["message_id"], record["replacement_id"]}.issubset(members))
        except (CleanupError, KeyError, TypeError, AttributeError):
            result["checks"]["thread_contains_original_and_replacement"] = False
        try:
            unchanged_original(api, record)
            result["checks"]["original_unchanged"] = True
        except CleanupError:
            result["checks"]["original_unchanged"] = False
        try:
            record["drive_verification"] = verify_selections(getattr(api, "drive", None), record["plan"]["selections"], record.get("drive_expectations") or record.get("drive_verification"))
            result["checks"]["retained_drive_files"] = True
        except CleanupError:
            result["checks"]["retained_drive_files"] = False
        result["passed"] = all(result["checks"].values())
        store.require_current(api)
        intent = store.get_intent(record.get('insert_intent_key', ''))
        result['checks']['current_policy_intent_binding'] = bool(intent and intent['action'] == 'gmail-insert'
            and all(intent['binding'].get(k) == v for k,v in mutation_binding(record).items()))
        result['passed'] = all(result['checks'].values())
    except CleanupError:
        result = {"passed": False, "checks": {"replacement_fetch": False},
                  "differences": {}, "observed": {"id": record["replacement_id"]}}
    record["verification"] = result
    record["state"] = "verified" if result["passed"] else "verify_failed"
    store.save(record, expected_state=current_state)
    return record


def create(api, store, manifest, message_id, execute=False):
    record = require_record(store, manifest, message_id)
    if record["state"] in {"trash_pending", "trashed", "trashed_verify_failed"}:
        raise CleanupError("Original trash lifecycle has started; inspect the saved audit")
    if record.get("replacement_id"):
        return verify(api, store, record) if execute else record
    if record["state"] != "planned":
        raise CleanupError("Insert outcome is uncertain; reconcile a replacement ID before retrying")
    if not execute:
        return record
    store.require_current(api)
    if not current_policy(record['plan']):
        raise CleanupError('Legacy plan requires explicit recovery/replanning; no insertion eligibility')
    original = unchanged_original(api, record)
    plan, raw = build_plan(original, record["plan"]["selections"], record["plan"]["operation_id"])
    if plan != record["plan"]:
        raise CleanupError("Regenerated replacement differs from approved plan")
    # A local manifest's attested hash is never sufficient for an actual insert.
    try:
        record["drive_verification"] = verify_selections(getattr(api, "drive", None), plan["selections"], record.get("drive_expectations") or record.get("drive_verification"))
        record.setdefault("drive_expectations", record["drive_verification"])
    except CleanupError as exc:
        record["drive_verification_failure"] = {"error": str(exc), **getattr(exc, "observation", {})}
        store.save(record, expected_state="planned")
        raise
    record.pop("drive_verification_failure", None)
    unchanged_original(api, record)  # downloads can take time; recheck before insertion intent
    record['insert_intent_key'] = store.intent('gmail-insert', mutation_binding(record))
    record["state"] = "insert_pending"
    store.save(record, expected_state="planned")
    # Any exception or crash from here is an uncertain insert. No blind retry.
    response = api.insert(raw, plan["original"]["labels"], plan["original"]["thread_id"])
    replacement_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(replacement_id, str) or not replacement_id or replacement_id == message_id:
        raise CleanupError("Insert returned no distinct replacement ID; reconcile before retrying")
    record["replacement_id"] = replacement_id
    record["state"] = "inserted"
    store.save(record, expected_state="insert_pending")
    return verify(api, store, record)


def reconcile(api, store, manifest, message_id, replacement_id, execute=False):
    if isinstance(replacement_id, list):
        if len(replacement_id) != 1:
            raise CleanupError('Zero or multiple candidates cannot resolve an uncertain insertion')
        replacement_id = replacement_id[0]
    record = require_record(store, manifest, message_id)
    if record["state"] not in {"planned", "insert_pending", "inserted", "verify_failed", "verified"}:
        raise CleanupError("No incomplete replacement insertion to reconcile")
    if record["replacement_id"] and record["replacement_id"] != replacement_id:
        raise CleanupError("A different replacement ID is already recorded")
    message = api.get(replacement_id)
    parsed = parse(message["raw_bytes"])
    if (message.get("id") != replacement_id or replacement_id == message_id
            or parsed.get_all(MARKER) != [record["plan"]["operation_id"]]):
        raise CleanupError("Candidate is not this operation's distinct replacement")
    if not current_policy(record['plan']) or store.recovery_error or not record.get('insert_intent_key'):
        result = check_replacement(message, record['plan'])
        observed = deepcopy(record)
        observed.update(state='recovery_only', replacement_id=replacement_id,
                        verification={**result, 'current_policy_eligible': False, 'historical_only': True})
        if execute:
            store.record_recovery(observed)
        return observed if execute else observed['verification']
    if not execute:
        original = unchanged_original(api, record)
        return check_replacement(message, record["plan"], approved_reconstruction(original, record["plan"]), original["raw_bytes"])
    old = record["state"]
    record.update(replacement_id=replacement_id, state="inserted")
    store.save(record, expected_state=old)
    return verify(api, store, record)


def trash_original(api, store, manifest, message_id, execute=False, enable_trash=False,
                   approve_plan=None, approve_replacement=None):
    record = require_record(store, manifest, message_id)
    if not execute:
        return record
    store.require_current(api)
    if not current_policy(record['plan']):
        raise CleanupError('Legacy historical verification does not grant current-policy Trash eligibility')
    if (not enable_trash or approve_plan != record["plan"]["plan_sha256"]
            or approve_replacement != record["replacement_id"] or not approve_replacement):
        raise CleanupError("Trash requires separate enablement and exact plan/replacement approval")
    if not getattr(api, "can_modify", False):
        raise CleanupError("Trash requires explicitly enabled gmail.modify capability")
    if record["state"] in {"trash_pending", "trashed", "trashed_verify_failed"}:
        return verify_after_trash(api, store, record)  # Read-only; never another write.
    if record["state"] != "verified" or not record.get("verification", {}).get("passed"):
        raise CleanupError("Successful replacement verification is required before trash")
    record = verify(api, store, record)  # Fresh original, replacement, thread and Drive checks.
    if not record["verification"]["passed"]:
        raise CleanupError("Fresh replacement verification failed; original remains untouched")
    original = unchanged_original(api, record)
    replacement = api.get(record["replacement_id"])
    if (replacement.get("id") != record["replacement_id"]
            or not check_replacement(replacement, record["plan"], approved_reconstruction(original, record["plan"]), original["raw_bytes"])["passed"]):
        raise CleanupError("Replacement changed immediately before Trash; original remains untouched")
    record["trash_audit"] = {"original": correspondence_observation(original),
                             "replacement_before_trash": correspondence_observation(replacement)}
    record["approval"] = {"action": "trash-original", "plan_sha256": approve_plan,
                          "replacement_id": approve_replacement}
    record['trash_intent_key'] = store.intent('gmail-trash', {**mutation_binding(record),
        'replacement_id': record['replacement_id'], 'approval': record['approval']})
    record["state"] = "trash_pending"
    store.save(record, expected_state="verified")
    api.trash(message_id)  # Exact message only; durable intent and no automatic retry.
    return verify_after_trash(api, store, record)


def correspondence_observation(message):
    """Requested audit metadata; never includes bodies, recipients or tokens."""
    parsed = parse(message["raw_bytes"])
    return {"id": message["id"], "thread_id": message.get("threadId"),
            "labels": sorted(message.get("labelIds", [])),
            "internal_date_ms": int(message["internalDate"]),
            "date_header_ms": date_ms(parsed), "size_bytes": len(message["raw_bytes"]),
            "raw_sha256": digest(message["raw_bytes"]),
            "headers": {name: [str(v) for v in parsed.get_all(name, [])]
                        for name in ("Date", "Message-ID", "In-Reply-To", "References")}}


def verify_after_trash(api, store, record):
    """Read-only postcheck/recovery: original must still exist, intact and in Trash."""
    if record["state"] not in {"trash_pending", "trashed", "trashed_verify_failed"}:
        raise CleanupError("No recorded Trash intent")
    old = record["state"]
    original = api.get(record["message_id"])  # 404/auth/transport errors never establish success.
    expected = record["plan"]["original"]
    if (original.get("id") != record["message_id"]
            or "TRASH" not in original.get("labelIds", [])
            or digest(original["raw_bytes"]) != expected["raw_sha256"]
            or original.get("threadId") != expected["thread_id"]
            or int(original["internalDate"]) != expected["internal_date_ms"]):
        raise CleanupError("Trash outcome is uncertain or original changed; inspect it before any further action")
    record.setdefault("trash_audit", {})["original_after_trash"] = correspondence_observation(original)
    checks = {"original_intact_in_trash": True}
    result = {"passed": False, "checks": checks, "differences": {}}
    try:
        replacement = api.get(record["replacement_id"])
        result = check_replacement(replacement, record["plan"], approved_reconstruction(original, record["plan"]), original["raw_bytes"])
        checks = result["checks"]
        checks["original_intact_in_trash"] = True
        checks["recorded_replacement_id"] = replacement.get("id") == record["replacement_id"]
        record["trash_audit"]["replacement_after_trash"] = correspondence_observation(replacement)
    except (CleanupError, KeyError, TypeError, ValueError):
        checks["replacement_fetch_and_verification"] = False
    try:
        thread = api.get_thread(expected["thread_id"])
        members = {m["id"] for m in thread.get("messages", [])}
        # Gmail may omit the trashed original from this thread view. Inspect it separately.
        checks["thread_contains_replacement"] = (thread.get("id") == expected["thread_id"]
                                                  and record["replacement_id"] in members)
    except (CleanupError, KeyError, TypeError, AttributeError):
        checks["thread_contains_replacement"] = False
    try:
        record["drive_verification"] = verify_selections(getattr(api, "drive", None), record["plan"]["selections"], record.get("drive_expectations") or record.get("drive_verification"))
        checks["retained_drive_files"] = True
    except CleanupError:
        checks["retained_drive_files"] = False
    result["checks"] = checks
    result["passed"] = all(checks.values())
    record["post_trash_verification"] = result
    record["state"] = "trashed" if result["passed"] else "trashed_verify_failed"
    store.save(record, expected_state=old)
    return record
