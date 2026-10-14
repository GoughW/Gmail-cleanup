"""Pinned directory descriptors and one alias policy for CLI inputs/state/outputs."""
from contextvars import ContextVar
import json
import os
from pathlib import Path
import stat

from .errors import CleanupError

ACTIVE = ContextVar("artifact_paths", default=None)


def durable_parents(parent):
    parent = Path(parent)
    if parent.is_dir():
        return
    durable_parents(parent.parent)
    fd = os.open(parent.parent.resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            os.mkdir(parent.name, mode=0o700, dir_fd=fd)
        except FileExistsError:
            if not parent.is_dir():
                raise
        os.fsync(fd)
    finally:
        os.close(fd)


def key(path):
    path = Path(path).expanduser().absolute()
    return str(path.parent.resolve() / path.name)


def identity(path, follow=True):
    try:
        info = os.stat(path, follow_symlinks=follow)
        return info.st_dev, info.st_ino
    except FileNotFoundError:
        return None


class Target:
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        durable_parents(self.path.parent)
        self.parent = self.path.parent.resolve()
        self.fd = os.open(self.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self.fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            os.close(self.fd)
            raise CleanupError("Artifact parent must be owned and not writable by other users")
        self.parent_identity = info.st_dev, info.st_ino
        self.name = self.path.name
        try:
            self.initial = self._leaf()
        except BaseException:
            os.close(self.fd)
            raise

    def _leaf(self):
        try:
            info = os.stat(self.name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CleanupError("Write target must be a regular, singly linked file, never a symlink")
        return info.st_dev, info.st_ino

    def check(self):
        if identity(self.path.parent) != self.parent_identity:
            raise CleanupError("Artifact parent changed after validation")
        if self._leaf() != self.initial:
            raise CleanupError("Artifact target changed after validation")

    def advance(self):
        self.initial = self._leaf()

    def close(self):
        os.close(self.fd)


class PathSession:
    def __init__(self, inputs=(), outputs=(), state=None, journal=None):
        self.inputs = [Path(p).expanduser().absolute() for p in inputs if p]
        self.state = Path(state).expanduser().absolute() if state else None
        self.journal = Path(journal or (str(self.state) + ".recovery")).absolute() if self.state else None
        self.reserved = self.inputs[:]
        if self.state:
            sensitive = [Path(str(self.state) + suffix) for suffix in ('', '-journal', '-wal', '-shm', '.lock')]
            for source in self.inputs:
                for storage in sensitive:
                    storage_identity = identity(storage)
                    if key(source) == key(storage) or (storage_identity is not None and storage_identity == identity(source)):
                        raise CleanupError('State/sidecar aliases a protected input')
                journal_root = Path(key(self.journal))
                if Path(key(source)) == journal_root or journal_root in Path(key(source)).parents:
                    raise CleanupError('Input aliases authoritative recovery storage')
            self.reserved += [Path(str(self.state) + suffix) for suffix in ("", "-journal", "-wal", "-shm", ".lock", ".inspection")]
        self.targets = {}
        self.managed = None
        try:
            if self.state:
                self.managed = Target(self.state)
                for suffix in ('', '-journal', '-wal', '-shm', '.lock'):
                    try:
                        info = os.stat(self.managed.name + suffix, dir_fd=self.managed.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.getuid() or info.st_mode & 0o077):
                        raise CleanupError('Managed state/sidecar must be private, regular and singly linked')
                if self.journal.exists() or self.journal.is_symlink():
                    info = self.journal.lstat()
                    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                            or info.st_mode & 0o077):
                        raise CleanupError('Recovery directory must be private, owned and not a symlink')
            for path in outputs:
                if path:
                    self.target(path)
        except BaseException:
            self.close()
            raise

    def target(self, path):
        canonical = key(path)
        if canonical in self.targets:
            self.targets[canonical].check()
            return self.targets[canonical]
        candidate = identity(path)
        for protected in self.reserved:
            if canonical == key(protected) or (candidate is not None and candidate == identity(protected)):
                raise CleanupError("Output aliases a protected input, state or SQLite sidecar")
        if self.journal:
            journal = Path(key(self.journal))
            if Path(canonical) == journal or journal in Path(canonical).parents:
                raise CleanupError("Output aliases authoritative recovery storage")
            # Existing journal files may also be hard-linked outside the tree.
            if self.journal.is_dir() and candidate is not None:
                for file in self.journal.iterdir():
                    if candidate == identity(file):
                        raise CleanupError("Output aliases a recovery record")
        for target in self.targets.values():
            if candidate is not None and candidate == identity(target.path):
                raise CleanupError("Outputs alias each other")
        target = Target(path)
        self.targets[canonical] = target
        return target

    def validate_outputs(self, outputs):
        seen = set()
        for path in outputs:
            if path:
                canonical = key(path)
                if canonical in seen:
                    raise CleanupError("Output paths collide")
                seen.add(canonical)

    def close(self):
        if self.managed is not None:
            self.managed.close()
            self.managed = None
        for target in self.targets.values():
            target.close()
        self.targets.clear()


def cli_paths(args):
    inputs = [getattr(args, name, None) for name in ("manifest", "credentials", "offline_sources", "ids")]
    outputs = [getattr(args, name, None) for name in ("output", "resolved_manifest")]
    if getattr(args, "command", None) == "auth":
        outputs.append(args.token)
    else:
        inputs.append(getattr(args, "token", None))
    try:
        inventory = getattr(args, "offline_sources", None)
        if inventory:
            data = json.loads(Path(inventory).read_text())
            inputs.extend(Path(inventory).absolute().parent / row["raw_file"] for row in data.values())
        if getattr(args, "command", None) == "register":
            index = Path(args.directory) / "fixtures.json"
            inputs.append(index)
            data = json.loads(index.read_text())
            inputs.extend(index.parent / row["raw_file"] for row in data["cases"].values())
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CleanupError('Cannot validate referenced local input paths') from None
    session = PathSession(inputs, outputs, getattr(args, "state", None), getattr(args, "journal", None))
    try:
        session.validate_outputs(outputs)
    except BaseException:
        session.close()
        raise
    return session
