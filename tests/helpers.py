from copy import deepcopy

from gmail_cleanup.errors import CleanupError
from gmail_cleanup.fixtures import fixture, TEST_MARKER
from gmail_cleanup.mime import date_ms, parse, parts, digest
from gmail_cleanup.drive import PURPOSE, FOLDER_TYPE


class FakeDrive:
    """Model retained binary files; hashes are computed from actual fake download bytes."""
    def __init__(self):
        self.files = {}
        self.payloads = {}
        self.creates = []
        self.downloads = []
        self.allocated = []
        self.lose_response = False
        self.reject_create = False
        self.corrupt_download = False
        self.change_during_download = False

    def add(self, file_id, payload=None, folder_id="fixture-folder", name="retained.bin"):
        metadata = {"id": file_id, "name": name, "mimeType": FOLDER_TYPE if payload is None else "application/octet-stream",
                    "trashed": False, "version": "1", "appProperties": {"purpose": PURPOSE}}
        if payload is not None:
            metadata.update(size=str(len(payload)), parents=[folder_id], sha256Checksum=digest(payload))
            metadata["appProperties"]["source_sha256"] = digest(payload)
            self.payloads[file_id] = payload
        self.files[file_id] = metadata

    def get(self, file_id):
        return deepcopy(self.files.get(file_id))

    def find(self, folder_id=None, source_hash=None, folders=False):
        return [deepcopy(item) for item in self.files.values() if not item.get("trashed")
                and item.get("appProperties", {}).get("purpose") == PURPOSE
                and ((folders and item["mimeType"] == FOLDER_TYPE)
                     or (not folders and folder_id in item.get("parents", [])
                         and item["appProperties"].get("source_sha256") == source_hash))]

    def allocate_id(self):
        file_id = "allocated" + str(len(self.allocated) + 1)
        self.allocated.append(file_id)
        return file_id

    def create(self, file_id, name, folder_id=None, payload=None):
        if self.reject_create:
            raise CleanupError("Simulated Drive upload failure")
        if file_id in self.files:
            raise CleanupError("Simulated ID conflict")
        self.add(file_id, payload, folder_id, name)
        self.creates.append(file_id)
        if self.lose_response:
            raise CleanupError("Simulated lost Drive create response")
        return self.get(file_id)

    def download_hash(self, file_id):
        self.downloads.append(file_id)
        payload = self.payloads[file_id]
        if self.change_during_download:
            self.files[file_id]["version"] = str(int(self.files[file_id]["version"]) + 1)
        if self.corrupt_download:
            payload += b"CORRUPTION"
        return digest(payload), len(payload)


def message(case="plain", message_id="abc123", run_id="offline"):
    raw, choices = fixture(case, run_id)
    result = {"id": message_id, "raw_bytes": raw, "threadId": "thread123",
              "internalDate": str(date_ms(parse(raw))),
              "labelIds": ["SENT", "STARRED"] if case == "sent" else ["INBOX", "UNREAD", "STARRED", "Label_test"]}
    manifest = {"version": 1, "allowlist": [message_id], "messages": {message_id: choices}}
    return result, manifest


class FakeGmail:
    """Observable in-memory mailbox. No networking, credentials or connector."""
    def __init__(self, messages):
        self.messages = deepcopy(messages)
        self.inserts = []
        self.trashes = []
        self.events = []
        self.can_modify = True
        self.reads = []
        self.lose_insert_response = False
        self.lose_trash_response = False
        self.reject_trash = False
        self.reject_insert = False
        self.corrupt = None
        self.fail_get = set()
        self.drive = FakeDrive()
        self.drive.add("fixture-folder")
        for item in messages.values():
            parsed = parse(item["raw_bytes"])
            marker = parsed.get(TEST_MARKER)
            if marker:
                run_id, case = str(marker).split(":", 1)
                attachment = next((p for _, p in parts(parsed) if p.get_content_disposition() == "attachment"), None)
                if attachment is not None:
                    self.drive.add(f"SYNTHETIC-{run_id}-{case}", attachment.get_payload(decode=True))

    def get(self, message_id):
        self.reads.append(message_id)
        self.events.append(("get", message_id))
        if message_id in self.fail_get:
            raise CleanupError("Simulated fetch failure")
        if message_id not in self.messages:
            raise CleanupError("Simulated missing ID")
        return deepcopy(self.messages[message_id])

    def insert(self, raw, labels, thread_id=None):
        if self.reject_insert:
            raise CleanupError("Simulated unsupported label/thread insertion")
        message_id = f"replacement{len(self.inserts) + 1}"
        item = {"id": message_id, "raw_bytes": raw, "threadId": thread_id or message_id,
                "internalDate": str(date_ms(parse(raw))), "labelIds": list(labels)}
        if self.corrupt:
            self.corrupt(item)
        self.messages[message_id] = item
        self.inserts.append(message_id)
        self.events.append(("insert", message_id))
        if self.lose_insert_response:
            raise CleanupError("Simulated lost insert response")
        return {"id": message_id}

    def get_thread(self, thread_id):
        return {"id": thread_id, "messages": [{"id": item["id"]} for item in self.messages.values()
                                               if item["threadId"] == thread_id]}

    def trash(self, message_id):
        if not self.can_modify or self.reject_trash:
            raise CleanupError("Simulated Trash rejection")
        self.events.append(("trash", message_id))
        self.trashes.append(message_id)
        self.messages[message_id]["labelIds"].append("TRASH")
        if self.lose_trash_response:
            raise CleanupError("Simulated lost trash response")
        return {"id": message_id}
