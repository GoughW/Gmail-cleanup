import json
from pathlib import Path
import re

from .errors import CleanupError
from .mime import validate_url

HEX = re.compile(r"[0-9a-f]{64}\Z")
PART = re.compile(r"0(?:\.[0-9]+)*\Z")


def load_manifest(path, allow_empty=False):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise CleanupError("Unsupported selection manifest version")
        allowed = data["allowlist"]
        messages = data["messages"]
        if (not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed)
                or any(not isinstance(x, str) or not x or not x.isalnum() for x in allowed)):
            raise CleanupError("An explicit, unique message-ID allowlist is required")
        if not isinstance(messages, dict) or set(messages) != set(allowed):
            raise CleanupError("Selections must cover exactly the explicit allowlist")
        for choices in messages.values():
            if not isinstance(choices, list) or (not choices and not allow_empty):
                raise CleanupError("Select at least one MIME attachment per allowed message")
            for choice in choices:
                if (set(choice) != {"part", "decoded_sha256", "retained_sha256", "drive_url"}
                        or not PART.fullmatch(choice["part"])
                        or not HEX.fullmatch(choice["decoded_sha256"])
                        or not HEX.fullmatch(choice["retained_sha256"])):
                    raise CleanupError("Malformed MIME part/hash selection")
                validate_url(choice["drive_url"])
        return data
    except CleanupError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise CleanupError("Cannot read a valid selection manifest") from None


def select(data, message_id):
    if message_id not in data["allowlist"]:
        raise CleanupError("Message ID is outside the explicit allowlist")
    return data["messages"][message_id]


def load_backup_manifest(path):
    """Version 2 names original MIME parts and hashes; Drive URLs are resolved automatically."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("version") != 2 or set(data) != {"version", "allowlist", "messages"}:
            raise CleanupError("Use a version-2 backup selection manifest")
        allowed, messages = data["allowlist"], data["messages"]
        if (not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed)
                or any(not isinstance(x, str) or not x.isalnum() for x in allowed)
                or not isinstance(messages, dict) or set(messages) != set(allowed)):
            raise CleanupError("Backup selections must cover exactly a unique message-ID allowlist")
        for choices in messages.values():
            if not isinstance(choices, list) or not choices:
                raise CleanupError("Explicitly select at least one attachment per message")
            for choice in choices:
                if (set(choice) not in ({"part", "decoded_sha256"}, {"part", "decoded_sha256", "drive_file_id"})
                        or not PART.fullmatch(choice["part"]) or not HEX.fullmatch(choice["decoded_sha256"])):
                    raise CleanupError("Invalid backup MIME part/hash selection")
                if "drive_file_id" in choice:
                    from .drive import valid_id
                    valid_id(choice["drive_file_id"])
        return data
    except CleanupError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise CleanupError("Cannot read a valid backup manifest") from None
