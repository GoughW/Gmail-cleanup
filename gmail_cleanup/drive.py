"""App-scoped Drive backups. Only create, list, read and download are exposed."""

from hashlib import sha256
from io import BytesIO
import re

from .api import DRIVE_SCOPE, DRIVE_READ_SCOPE
from .errors import CleanupError
from .mime import digest, drive_file_id

FOLDER_TYPE = "application/vnd.google-apps.folder"
PURPOSE = "gmail-cleanup-backups-v1"
FIELDS = "id,name,mimeType,trashed,parents,size,sha256Checksum,md5Checksum,sha1Checksum,version,appProperties,createdTime,modifiedTime,viewedByMeTime,webViewLink"
FILE_ID = re.compile(r"[A-Za-z0-9_-]+\Z")


class BackupVerificationError(CleanupError):
    def __init__(self, reason, observation):
        super().__init__(reason)
        self.observation = observation


def valid_id(value):
    if not isinstance(value, str) or not FILE_ID.fullmatch(value):
        raise CleanupError("Invalid Drive file/folder ID")
    return value


def file_id_from_url(url):
    return valid_id(drive_file_id(url))


class Drive:
    def __init__(self, credentials, http):
        scopes = set(credentials.granted_scopes if credentials.granted_scopes is not None
                     else (credentials.scopes or []))
        if not scopes & {DRIVE_SCOPE, DRIVE_READ_SCOPE}:
            raise CleanupError("Drive access is missing: obtain a new desktop OAuth grant including drive.file, enable Drive API, and publish the environment")
        from googleapiclient.discovery import build
        self.service = build("drive", "v3", http=http, cache_discovery=False, static_discovery=True)
        self.can_upload = DRIVE_SCOPE in scopes

    @staticmethod
    def _execute(request, missing_ok=False):
        try:
            result = request.execute(num_retries=0)
            if not isinstance(result, dict):
                raise CleanupError("Invalid Drive API response")
            return result
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if missing_ok and status == 404:
                return None
            suffix = f" (HTTP {status})" if isinstance(status, int) else ""
            raise CleanupError("Drive API request failed" + suffix) from None

    def get(self, file_id):
        return self._execute(self.service.files().get(fileId=valid_id(file_id), fields=FIELDS), missing_ok=True)

    def find(self, folder_id=None, source_hash=None, folders=False):
        query = f"trashed = false and appProperties has {{ key='purpose' and value='{PURPOSE}' }}"
        if folders:
            query += f" and mimeType = '{FOLDER_TYPE}'"
        else:
            query += f" and '{valid_id(folder_id)}' in parents"
            if not re.fullmatch(r"[0-9a-f]{64}", source_hash or ""):
                raise CleanupError("Invalid source hash for Drive lookup")
            query += f" and appProperties has {{ key='source_sha256' and value='{source_hash}' }}"
        results, page = [], None
        while True:
            response = self._execute(self.service.files().list(
                q=query, spaces="drive", pageSize=100, pageToken=page,
                fields=f"files({FIELDS}),nextPageToken,incompleteSearch"))
            if response.get("incompleteSearch"):
                raise CleanupError("Drive search is incomplete; provide an explicit destination")
            results.extend(response.get("files", []))
            page = response.get("nextPageToken")
            if not page:
                return results

    def allocate_id(self):
        if not self.can_upload:
            raise CleanupError("Creating a Drive backup requires drive.file authorization")
        result = self._execute(self.service.files().generateIds(count=1, space="drive", type="files"))
        if not isinstance(result.get("ids"), list) or len(result["ids"]) != 1:
            raise CleanupError("Drive did not return one pre-generated file ID")
        return valid_id(result["ids"][0])

    def create(self, file_id, name, folder_id=None, payload=None):
        if not self.can_upload:
            raise CleanupError("Creating a Drive backup requires drive.file authorization")
        metadata = {"id": valid_id(file_id), "name": name, "appProperties": {"purpose": PURPOSE}}
        kwargs = {}
        if payload is None:
            metadata["mimeType"] = FOLDER_TYPE
        else:
            from googleapiclient.http import MediaIoBaseUpload
            metadata.update(mimeType="application/octet-stream", parents=[valid_id(folder_id)])
            metadata["appProperties"]["source_sha256"] = digest(payload)
            kwargs["media_body"] = MediaIoBaseUpload(BytesIO(payload), mimetype="application/octet-stream", resumable=False)
        return self._execute(self.service.files().create(body=metadata, fields=FIELDS, **kwargs))

    def download_hash(self, file_id):
        # Hash downloads incrementally. No file contents are logged or stored in git.
        from googleapiclient.http import MediaIoBaseDownload
        class HashSink:
            def __init__(self):
                self.hash = sha256()
                self.size = 0
            def write(self, data):
                self.hash.update(data)
                self.size += len(data)
        sink = HashSink()
        try:
            request = self.service.files().get_media(fileId=valid_id(file_id))
            download = MediaIoBaseDownload(sink, request, chunksize=1024 * 1024)
            done = False
            while not done:
                _, done = download.next_chunk(num_retries=0)
            return sink.hash.hexdigest(), sink.size
        except Exception:
            raise CleanupError("Drive file download failed; backup is not verified") from None


def metadata_changes(before, after):
    """Classify metadata changes; downloaded bytes, not server counters, prove content."""
    changes = []
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [{"field": "file_presence", "before": bool(before), "after": bool(after),
                 "classification": "integrity-relevant", "blocker": True}]
    for field in sorted(set(before) | set(after)):
        previous, current = before.get(field), after.get(field)
        if previous == current:
            continue
        blocker = field in {"id", "name", "size", "parents", "trashed"}
        if field == "parents" and isinstance(previous, list) and isinstance(current, list):
            blocker = sorted(previous) != sorted(current)
        if field == "size":
            try:
                blocker = int(previous) != int(current)
            except (ValueError, TypeError):
                blocker = True
        if field == "mimeType":
            blocker = (not isinstance(current, str)
                       or current.startswith("application/vnd.google-apps."))
        changes.append({"field": field, "before": previous, "after": current,
                        "classification": "integrity-relevant" if blocker else "benign/expected metadata mutation",
                        "blocker": blocker})
    return changes


def metadata_errors(item, file_id, expected_name, expected_parents, expected_size):
    if not isinstance(item, dict):
        return ["file_presence"]
    errors = []
    if item.get("id") != file_id:
        errors.append("id")
    if not expected_name or item.get("name") != expected_name:
        errors.append("name")
    if item.get("trashed") is not False:
        errors.append("trashed")
    mime = item.get("mimeType")
    if not isinstance(mime, str) or not mime or mime.startswith("application/vnd.google-apps."):
        errors.append("binary_file_type")
    parents = item.get("parents")
    if (not expected_parents or not isinstance(parents, list)
            or not all(isinstance(p, str) and p for p in parents)
            or sorted(parents) != sorted(expected_parents)):
        errors.append("parents")
    try:
        if int(item["size"]) < 0 or int(item["size"]) != expected_size:
            errors.append("size")
    except (KeyError, ValueError, TypeError):
        errors.append("size")
    return errors


def verify_file(drive, file_id, source_hash, source_size=None, folder_id=None,
                expected_name=None, expected_parents=None):
    file_id = valid_id(file_id)
    before = drive.get(file_id)
    if not isinstance(before, dict):
        raise BackupVerificationError("Retained Drive file is missing or inaccessible",
                                      {"file_id": file_id, "verified": False, "metadata_before": before})
    intended_name = expected_name if expected_name is not None else before.get("name")
    intended_parents = expected_parents if expected_parents is not None else before.get("parents", [])
    if not isinstance(intended_parents, list) or not all(isinstance(p, str) and p for p in intended_parents):
        intended_parents = []
    try:
        expected_size = int(before["size"]) if source_size is None else source_size
    except (KeyError, ValueError, TypeError):
        expected_size = None
    observation = {"file_id": file_id, "drive_url": None,
                   "source_sha256": source_hash, "downloaded_sha256": None,
                   "name": intended_name, "parents": intended_parents,
                   "expected_size_bytes": expected_size, "version": before.get("version"),
                   "verified": False, "metadata_before": before,
                   "method": "files.get alt=media; SHA-256 of downloaded bytes"}
    errors = metadata_errors(before, file_id, intended_name, intended_parents, expected_size)
    if folder_id and folder_id not in intended_parents:
        errors.append("intended_folder")
    if errors:
        observation["integrity_errors"] = {"before": errors}
        raise BackupVerificationError("Drive file identity/destination/size check failed: " + ", ".join(errors), observation)
    actual, size = drive.download_hash(file_id)
    after = drive.get(file_id)
    observation.update(downloaded_sha256=actual, size_bytes=size, metadata_after=after,
                       metadata_changes=metadata_changes(before, after))
    errors = metadata_errors(after, file_id, intended_name, intended_parents, expected_size)
    observation["checks"] = {"file_identity_name_destination_size_before": True,
                             "file_identity_name_destination_size_after": not errors,
                             "downloaded_sha256_matches_source": actual == source_hash,
                             "downloaded_size_matches_source_and_metadata": size == expected_size}
    observation["downloads"] = [{"sha256": actual, "size_bytes": size}]
    if observation["metadata_changes"] and not errors and actual == source_hash and size == expected_size:
        # Metadata settling is allowed. A bounded fresh download after the change
        # distinguishes settling from a genuine same-size content update. No wait,
        # mutation or requirement that counters stop changing is introduced.
        again, again_size = drive.download_hash(file_id)
        final = drive.get(file_id)
        final_errors = metadata_errors(final, file_id, intended_name, intended_parents, expected_size)
        observation.update(metadata_after_recheck=final,
                           metadata_recheck_changes=metadata_changes(after, final))
        observation["downloads"].append({"sha256": again, "size_bytes": again_size})
        observation["checks"].update(
            fresh_download_after_metadata_change_matches_source=again == source_hash and again_size == expected_size,
            file_identity_name_destination_size_after_recheck=not final_errors)
        errors += final_errors
    # These checksums/counters can lag post-upload processing. Record them as advisory;
    # the independent SHA-256 of the complete downloaded bytes is authoritative.
    observation["advisory_checksum_matches_download"] = {
        stage: item.get("sha256Checksum") == actual if item and item.get("sha256Checksum") else None
        for stage, item in (("before", before), ("after", after))}
    if errors or not all(observation["checks"].values()):
        observation["integrity_errors"] = {"after": errors}
        raise BackupVerificationError("Downloaded Drive file failed identity/destination/size/SHA-256 verification", observation)
    # Use the latest verified metadata; webViewLink may settle after upload too.
    # Never reconstruct a link and drop Drive's resource key or other query data.
    latest = observation.get("metadata_after_recheck", after)
    link = latest.get("webViewLink")
    try:
        if file_id_from_url(link) != file_id:
            raise CleanupError("Drive viewer URL identifies a different file")
    except CleanupError:
        raise BackupVerificationError("Drive returned no valid canonical webViewLink for the retained file", observation) from None
    observation.update(drive_url=link, web_view_link=link, link_source="Drive files.webViewLink")
    observation["verified"] = True
    return observation


class Backups:
    def __init__(self, drive, store, recover_upload=False):
        self.drive, self.store = drive, store
        self.recover_upload = recover_upload

    def _create_once(self, key, name, folder_id, payload, allow_upload):
        record = self.store.get_backup(key)
        if record is None:
            if not allow_upload:
                raise CleanupError("No retained copy found; --upload-backups enables Drive backup creation only")
            allocation = self.store.allocation_for(key)
            if allocation:
                if not allocation.get('result', {}).get('file_id'):
                    raise CleanupError('Allocation outcome uncertain; explicit recovery required')
                file_id = allocation['result']['file_id']
            else:
                intent = self.store.intent('drive-allocate', {'backup_key': key, 'name': name,
                                           'folder_id': folder_id, 'source_sha256': digest(payload) if payload is not None else 'folder'})
                file_id = self.drive.allocate_id()
                self.store.complete_intent(intent, {'file_id': file_id})
            record = {"state": "upload_pending", "file_id": file_id, "expected_name": name}
            # Persist the pre-generated ID BEFORE create; reuse it after a lost response.
            self.store.save_backup(key, record, new=True)
        file_id = record["file_id"]
        item = self.drive.get(file_id)
        if item is None:
            if record["state"] != "upload_pending":
                raise CleanupError("A previously created Drive backup disappeared; inspect it")
            if not allow_upload:
                raise CleanupError("Drive upload is incomplete; recorded file ID requires recovery")
            try:
                self.store.intent('drive-create-recovery' if self.recover_upload else 'drive-create', {'backup_key': key, 'file_id': file_id, 'name': name,
                    'folder_id': folder_id, 'source_sha256': digest(payload) if payload is not None else 'folder'})
                self.drive.create(file_id, name, folder_id, payload)
            except CleanupError:
                if self.drive.get(file_id) is None:
                    raise
            item = self.drive.get(file_id)
            if item is None:
                raise CleanupError("Drive create response could not be confirmed; ID remains recorded")
        if record["state"] == "upload_pending":
            record["state"] = "stored"
            self.store.save_backup(key, record, expected_state="upload_pending")
        return file_id

    def folder(self, folder_id=None, allow_upload=False):
        if folder_id is None:
            recorded = self.store.get_backup("default-folder")
            if recorded:
                folder_id = self._create_once("default-folder", "Gmail attachment backups", None, None, allow_upload)
            else:
                found = self.drive.find(folders=True)
                if len(found) > 1:
                    raise CleanupError("Multiple backup folders found; specify --drive-folder-id")
                if found:
                    folder_id = found[0]["id"]
                    self.store.save_backup("default-folder", {"state": "stored", "file_id": folder_id}, new=True)
                else:
                    folder_id = self._create_once("default-folder", "Gmail attachment backups", None, None, allow_upload)
        item = self.drive.get(valid_id(folder_id))
        if (not item or item.get("id") != folder_id or item.get("trashed")
                or item.get("mimeType") != FOLDER_TYPE):
            raise CleanupError("Intended Drive folder is missing, trashed or inaccessible")
        return folder_id

    def ensure(self, payload, filename, folder_id=None, file_id=None, allow_upload=False):
        source_hash = digest(payload)
        if file_id:
            return self._verify_recorded("file:" + valid_id(file_id) + ":" + source_hash,
                                         file_id, payload, folder_id, None)
        folder_id = self.folder(folder_id, allow_upload)
        key = folder_id + ":" + source_hash
        # Resume recorded intent before search to avoid losing a pending upload.
        record = self.store.get_backup(key)
        if record:
            file_id = self._create_once(key, filename, folder_id, payload, allow_upload)
        else:
            found = self.drive.find(folder_id, source_hash)
            # A tagged but corrupt copy is a failure, not permission to hide it with a new upload.
            if len(found) > 1:
                raise CleanupError("Multiple tagged counterparts found; supply an intended Drive file ID")
            file_id = found[0]["id"] if found else self._create_once(
                key, filename, folder_id, payload, allow_upload)
        return self._verify_recorded(key, file_id, payload, folder_id,
                                     (record.get("expected_name") or record.get("verification", {}).get("name") or filename) if record else None)

    def _verify_recorded(self, key, file_id, payload, folder_id, expected_name=None):
        record = self.store.get_backup(key)
        if record is None:
            record = {"state": "stored", "file_id": file_id}
            self.store.save_backup(key, record, new=True)
        if record["file_id"] != file_id:
            raise CleanupError("Saved backup file identity differs from the intended counterpart")
        old = record["state"]
        try:
            prior = record.get("verification", {})
            proof = verify_file(self.drive, file_id, digest(payload), len(payload), folder_id,
                                expected_name=record.get("expected_name") or prior.get("name") or expected_name,
                                expected_parents=prior.get("parents"))
        except CleanupError as exc:
            record.update(state="verify_failed", verification={"verified": False, "error": str(exc),
                          **getattr(exc, "observation", {})})
            self.store.save_backup(key, record, expected_state=old)
            raise
        record.update(state="verified", verification=proof)
        self.store.save_backup(key, record, expected_state=old)
        return proof


def verify_selections(drive, selections, expectations=None):
    if drive is None:
        raise CleanupError("Automatic Drive download verification is required before mailbox modification")
    expected_by_id = {proof["file_id"]: proof for proof in (expectations or [])}
    proofs = []
    for choice in selections:
        file_id = file_id_from_url(choice["drive_url"])
        expected = expected_by_id.get(file_id, {})
        proofs.append(verify_file(drive, file_id, choice["decoded_sha256"],
                      source_size=expected.get("expected_size_bytes", expected.get("size_bytes")),
                      expected_name=expected.get("name"), expected_parents=expected.get("parents")))
    return proofs
