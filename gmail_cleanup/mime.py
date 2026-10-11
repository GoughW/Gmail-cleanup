"""Conservative MIME transformation; part paths address the original raw MIME tree."""

from copy import deepcopy
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import escape, unescape
import json
from urllib.parse import urlsplit, unquote, parse_qs

from .errors import CleanupError, Unsupported

MARKER = "X-Gmail-Attachment-Cleanup"
SIGNATURE_HEADERS = {"dkim-signature", "domainkey-signature", "arc-seal", "arc-message-signature",
                     "x-google-dkim-signature"}
# These authenticate the delivered bytes, not a locally reconstructed copy.
# Remove the complete ARC chain and authentication assertions with the signatures.
TRANSPORT_AUTH_HEADERS = SIGNATURE_HEADERS | {
    "authentication-results", "arc-authentication-results", "received-spf",
    "x-google-original-authentication-results"}
# Only root delivery metadata may differ after mailbox insertion. This is not
# used when stripping attachments or comparing headers on nested MIME parts.
SERVER_TRANSPORT_HEADERS = TRANSPORT_AUTH_HEADERS | {"received"}


def semantic_root(msg):
    copy = deepcopy(msg)
    for name in SERVER_TRANSPORT_HEADERS:
        del copy[name]
    return copy


def all_header_differences(before, after):
    """Audit every MIME part, including removed resources, without header values."""
    old, new = dict(parts(before)), dict(parts(after))
    changes = []
    for path in dict.fromkeys([*old, *new]):
        previous = list(old[path].items()) if path in old else []
        current = list(new[path].items()) if path in new else []
        names = dict.fromkeys(k.lower() for k, _ in previous + current)
        for name in names:
            left = [digest(str(v).encode("utf-8")) for k, v in previous if k.lower() == name]
            right = [digest(str(v).encode("utf-8")) for k, v in current if k.lower() == name]
            if left != right:
                changes.append({"part": path, "name": name, "before_value_sha256": left,
                                "after_value_sha256": right,
                                "classification": "transport" if path == "0" and name in SERVER_TRANSPORT_HEADERS else "semantic",
                                "action": "add" if not left else "remove" if not right else "change"})
        left_sequence = [[k, digest(str(v).encode("utf-8"))] for k, v in previous]
        right_sequence = [[k, digest(str(v).encode("utf-8"))] for k, v in current]
        if left_sequence != right_sequence:
            changes.append({"part": path, "action": "header_sequence_change",
                            "before": left_sequence, "after": right_sequence})
    return changes


def transport_header_changes(msg):
    return [{"name": key, "value_sha256": digest(str(value).encode("utf-8")),
             "original_index": index, "action": "remove"}
            for index, (key, value) in enumerate(msg.items())
            if key.lower() in TRANSPORT_AUTH_HEADERS]


def without_transport_auth(msg):
    copy = deepcopy(msg)
    for name in TRANSPORT_AUTH_HEADERS:
        del copy[name]
    return copy
UNIQUE_HEADERS = {"from", "sender", "to", "cc", "bcc", "subject", "date", "message-id",
                  "in-reply-to", "references", "mime-version", "content-type",
                  "content-transfer-encoding", "content-disposition", "content-id"}


def digest(data):
    return sha256(data).hexdigest()


def json_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=True,
                             separators=(",", ":")).encode())


def parts(message, path="0"):
    yield path, message
    if message.is_multipart():
        for index, child in enumerate(message.get_payload()):
            yield from parts(child, f"{path}.{index}")


def parse(raw):
    try:
        msg = BytesParser(policy=policy.SMTP).parsebytes(raw)
        # Parsing is intentionally eager: header and transfer-encoding defects can
        # otherwise appear only while traversing or decoding a payload.
        for _, part in parts(msg):
            for key in part.keys():
                header = part[key]
                if getattr(header, "defects", ()):
                    raise Unsupported("Malformed MIME/header structure")
            if not part.is_multipart():
                part.get_payload(decode=True)
            if part.defects:
                raise Unsupported("Malformed MIME/transfer encoding")
        return msg
    except CleanupError:
        raise
    except (ValueError, TypeError, UnicodeError, LookupError):
        raise Unsupported("Cannot parse MIME safely") from None


def body(part):
    return (not part.is_multipart() and part.get_content_type() in {"text/plain", "text/html"}
            and part.get_content_disposition() is None and part.get_filename() is None
            and part.get("Content-ID") is None)


def body_text(part):
    try:
        return part.get_payload(decode=True).decode(part.get_content_charset() or "ascii")
    except (UnicodeError, LookupError, AttributeError):
        raise Unsupported("Body charset cannot be decoded without loss") from None


def header_fingerprint(msg, exclude_mime=False):
    # Keep duplicate headers and their order. Never log addresses or subject text.
    values = []
    for key, value in msg.items():
        if exclude_mime and (key.lower().startswith("content-") or key.lower() == "mime-version"):
            continue
        values.append([key.lower(), str(value)])
    return json_digest(values)


def inventory(msg):
    result = []
    for path, part in parts(msg):
        item = {"part": path, "content_type": part.get_content_type(),
                "headers_sha256": header_fingerprint(part), "multipart": part.is_multipart()}
        if not part.is_multipart():
            payload = part.get_payload(decode=True)
            item.update(decoded_sha256=digest(payload), decoded_size=len(payload),
                        filename=part.get_filename(), disposition=part.get_content_disposition())
        result.append(item)
    return result


def changed_headers(original, reconstructed):
    """Audit header edits without recording header values. Body/container paths stay stable."""
    after = dict(parts(reconstructed))
    changes = []
    for path, before_part in parts(original):
        if not (before_part.is_multipart() or body(before_part)):
            continue  # selected/unselected resource leaves are audited by their inventories
        after_part = after[path]
        names = dict.fromkeys(key.lower() for key in before_part.keys() + after_part.keys())
        for name in names:
            previous = [digest(str(value).encode("utf-8")) for value in before_part.get_all(name, [])]
            current = [digest(str(value).encode("utf-8")) for value in after_part.get_all(name, [])]
            if previous != current:
                changes.append({"part": path, "name": name, "before_value_sha256": previous,
                                "after_value_sha256": current,
                                "action": "add" if not previous else "remove" if not current else "change"})
    return changes


def date_ms(msg):
    try:
        value = parsedate_to_datetime(str(msg["Date"]))
        if value.tzinfo is None:
            raise ValueError
        return int(value.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        raise Unsupported("A valid Date header with timezone is required") from None


def validate_structure(msg):
    if msg.get(MARKER):
        raise Unsupported("Already marked as a cleanup replacement")
    for required in ("From", "Date", "Message-ID", "Subject"):
        if not msg.get(required):
            raise Unsupported("Missing required correspondence header")
    date_ms(msg)
    bodies = []
    for path, part in parts(msg):
        names = [key.lower() for key in part.keys()]
        if any(names.count(key) > 1 for key in UNIQUE_HEADERS | {k for k in names if k.startswith("content-")}):
            raise Unsupported("Ambiguous duplicate MIME/correspondence headers")
        if path != "0" and TRANSPORT_AUTH_HEADERS.intersection(names):
            raise Unsupported("Transport authentication headers inside a MIME part are ambiguous")
        content_type = part.get_content_type()
        if content_type.startswith("message/"):
            raise Unsupported("Nested attached email/message parts are unsupported")
        if (content_type in {"multipart/signed", "multipart/encrypted", "application/pgp-encrypted",
                             "application/pkcs7-mime", "application/x-pkcs7-mime",
                             "application/pkcs7-signature", "application/x-pkcs7-signature"}
                or part.get_param("smime-type") is not None):
            raise Unsupported("Signed/encrypted MIME is unsupported")
        if part.is_multipart():
            if content_type not in {"multipart/mixed", "multipart/alternative", "multipart/related"}:
                raise Unsupported("Unsupported multipart structure")
            if part.get_content_disposition() or part.get_filename():
                raise Unsupported("Attached/disposition-bearing multipart is ambiguous")
            if part.preamble and part.preamble.strip():
                raise Unsupported("Nonempty MIME preamble is unsupported")
            if part.epilogue and part.epilogue.strip():
                raise Unsupported("Nonempty MIME epilogue is unsupported")
            if content_type == "multipart/alternative":
                children = part.get_payload()
                kinds = [child.get_content_type() for child in children]
                if len(kinds) != len(set(kinds)):
                    raise Unsupported("Ambiguous alternative body choices")
                for child in children:
                    if not (body(child) or child.get_content_type() == "multipart/related"):
                        raise Unsupported("Unsupported alternative body choice")
        elif body(part):
            text = body_text(part)
            if any(marker in text for marker in ("-----BEGIN PGP MESSAGE-----",
                                                 "-----BEGIN PGP SIGNED MESSAGE-----")):
                raise Unsupported("Inline PGP signed/encrypted correspondence is unsupported")
            bodies.append((path, part))
        elif not (part.get_content_disposition() in {"attachment", "inline"}
                  or part.get_filename() or part.get("Content-ID")):
            raise Unsupported("Unclassified MIME leaf")
    # One body per format is deliberately narrow; do not attach notes to text
    # that might actually be part of a nested forward or unrelated body fragment.
    if not bodies or len({p.get_content_type() for _, p in bodies}) != len(bodies):
        raise Unsupported("Missing or ambiguous correspondence bodies")
    _validate_layout(msg)
    return bodies


def _validate_body_tree(part):
    if body(part):
        return
    kind = part.get_content_type()
    children = part.get_payload() if part.is_multipart() else []
    if kind == "multipart/alternative" and children:
        for child in children:
            if child.get_content_type() == "multipart/alternative":
                raise Unsupported("Nested alternative bodies are ambiguous")
            _validate_body_tree(child)
        return
    if kind == "multipart/related" and children:
        start = part.get_param("start")
        if start is not None:
            raise Unsupported("Explicit related-root selection is unsupported")
        if not body(children[0]):
            raise Unsupported("Related MIME root must be an explicit text body")
        for child in children[1:]:
            if (child.is_multipart() or not child.get("Content-ID")
                    or child.get_content_disposition() not in {None, "inline"}
                    or child.get_content_maintype() != "image"):
                raise Unsupported("Ambiguous/non-image related MIME resource")
        return
    raise Unsupported("Ambiguous correspondence body layout")


def _validate_layout(msg):
    if msg.get_content_type() != "multipart/mixed":
        _validate_body_tree(msg)
        return
    children = msg.get_payload()
    if not children:
        raise Unsupported("Empty mixed MIME")
    _validate_body_tree(children[0])
    for child in children[1:]:
        if child.is_multipart() or body(child):
            raise Unsupported("Mixed MIME contains an ambiguous additional body/container")
        if not (child.get_content_disposition() == "attachment"
                or (child.get("Content-ID") and child.get_content_maintype() == "image")):
            raise Unsupported("Mixed MIME contains an unclassified resource")


def drive_file_id(value):
    """Validate supported Drive viewer URLs without discarding canonical query data."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise CleanupError("Invalid retained Drive URL")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname == "drive.google.com"
                 and not parsed.username and not parsed.password and parsed.port is None
                 and not parsed.fragment)
        file_id = ""
        if parsed.path.startswith("/file/d/"):
            file_id, _, suffix = parsed.path.removeprefix("/file/d/").partition("/")
            valid &= suffix.rstrip("/") in {"", "view", "preview"}
        elif parsed.path == "/open":
            ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
            file_id = ids[0] if len(ids) == 1 else ""
        else:
            valid = False
        valid &= bool(file_id) and all(c.isascii() and (c.isalnum() or c in "_-") for c in file_id)
    except ValueError:
        valid = False
    if not valid:
        raise CleanupError("Use a valid HTTPS Drive file viewer URL")
    return file_id


def validate_url(value):
    drive_file_id(value)


def removable_attachment(part, message):
    if part.is_multipart() or part.get_content_disposition() != "attachment":
        return False
    cid = part.get("Content-ID")
    if cid is None:
        return True
    # Gmail gives ordinary attachments Content-ID too. An explicitly attached
    # leaf in a supported layout is removable only if no body references its CID.
    value = str(cid).strip()
    if not (value.startswith("<") and value.endswith(">") and value[1:-1]
            and not any(c.isspace() or c in "<>" for c in value[1:-1])):
        return False
    target = "cid:" + value[1:-1].casefold()
    return not any(target in unquote(unescape(body_text(p))).casefold()
                   for _, p in parts(message) if body(p))


def transform(raw, selections, operation_id):
    original = parse(raw)
    validate_structure(original)
    indexed = dict(parts(original))
    selected = set()
    removed = []
    for choice in selections:
        path = choice["part"]
        if path in selected:
            raise CleanupError("Duplicate selected MIME part")
        selected.add(path)
        part = indexed.get(path)
        if part is None or part.is_multipart():
            raise CleanupError("Selected MIME part does not identify a leaf")
        if not removable_attachment(part, original):
            raise Unsupported("Only explicit non-inline attachment leaves may be removed")
        filename = part.get_filename()
        if not filename or any(ord(c) < 32 for c in filename):
            raise Unsupported("Selected attachment needs a readable filename without control characters")
        actual_hash = digest(part.get_payload(decode=True))
        if actual_hash != choice["decoded_sha256"] or actual_hash != choice["retained_sha256"]:
            raise CleanupError("Decoded attachment and retained-file SHA-256 must match exactly")
        validate_url(choice["drive_url"])
        removed.append({"part": path, "filename": filename, "decoded_sha256": actual_hash,
                        "retained_sha256": choice["retained_sha256"], "drive_url": choice["drive_url"],
                        "decoded_size": len(part.get_payload(decode=True))})
    if not removed:
        raise CleanupError("No attachments explicitly selected")
    modified = without_transport_auth(original)
    for path, part in list(parts(modified)):
        if part.is_multipart():
            part.set_payload([child for i, child in enumerate(part.get_payload())
                              if f"{path}.{i}" not in selected])
    text_note = "\n\n[Attachment cleanup: retained copies; SHA-256 recorded below]\n"
    text_note += "\n".join(f"Removed attachment: {r['filename']}\nRetained copy: {r['drive_url']}\nSHA-256: {r['retained_sha256']}" for r in removed)
    html_note = '<hr><div><p>Attachment cleanup: retained copies; SHA-256 recorded below</p><ul>'
    html_note += "".join(f'<li>Removed attachment: {escape(r["filename"])}; retained copy: '
                         f'<a href="{escape(r["drive_url"], quote=True)}">{escape(r["drive_url"])}</a>'
                         f'; SHA-256: {r["retained_sha256"]}</li>' for r in removed)
    html_note += "</ul></div>"
    for _, part in parts(modified):
        if body(part):
            subtype = part.get_content_subtype()
            original_text = body_text(part)
            extra_headers = [(key, str(value)) for key, value in part.items()
                             if key.lower() not in {"content-type", "content-transfer-encoding", "mime-version"}]
            # set_content adds a newline; make it explicit for deterministic verification.
            text = original_text + (text_note if subtype == "plain" else "\n" + html_note)
            part.set_content(text, subtype=subtype, charset="utf-8", cte="base64")
            for key, value in extra_headers:
                if key not in part:
                    part[key] = value
    modified[MARKER] = operation_id
    result = modified.as_bytes(policy=policy.SMTP)
    # Serialize and parse before fingerprinting: generator normalizes header folding.
    canonical = parse(result)
    # Match resources in traversal order to handle duplicate filenames/equal hashes.
    original_resources = [(p.get_content_type(), header_fingerprint(p), digest(p.get_payload(decode=True)))
                          for path, p in parts(original) if not p.is_multipart() and not body(p) and path not in selected]
    result_resources = [(p.get_content_type(), header_fingerprint(p), digest(p.get_payload(decode=True)))
                        for _, p in parts(canonical) if not p.is_multipart() and not body(p)]
    if original_resources != result_resources:
        raise Unsupported("Serialization changes an unselected attachment/inline resource")
    original_bodies = {p.get_content_type(): body_text(p) for _, p in parts(original) if body(p)}
    for _, part in parts(canonical):
        if body(part) and not body_text(part).startswith(original_bodies[part.get_content_type()]):
            raise Unsupported("Serialization changes original body text")
    if header_fingerprint(without_transport_auth(original), exclude_mime=True) != header_fingerprint(
            _without_marker(canonical), exclude_mime=True):
        raise Unsupported("Serialization changes correspondence headers")
    return result, removed, canonical


def _without_marker(msg):
    copy = deepcopy(msg)
    del copy[MARKER]
    return copy
