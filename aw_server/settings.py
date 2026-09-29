import copy
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by Windows builds
    fcntl = None
    import msvcrt

from aw_core.dirs import get_config_dir

from .rules_settings import (
    LEGACY_RULE_KEYS,
    MAX_SAFE_REVISION,
    RULES_KEY,
    RulesConflictError,
    RulesValidationError,
    compatibility_projection,
    translate_legacy_write,
    validate_rules_envelope,
)


class Settings:
    """JSON settings storage with atomic writes and revisioned rules operations.

    The sidecar flock coordinates separate aw-server processes while ``_mutex``
    coordinates threads in one process.  Every operation reloads after taking
    both locks so a process never overwrites another process's newer settings.
    """

    def __init__(self, testing: bool):
        filename = "settings.json" if not testing else "settings-testing.json"
        self.config_file = Path(get_config_dir("aw-server")) / filename
        self.lock_file = self.config_file.with_name(self.config_file.name + ".lock")
        self._mutex = RLock()
        self.data: Dict[str, Any] = {}
        self.load()

    def __getitem__(self, key):
        return self.get(key)

    def __setitem__(self, key, value):
        return self.set(key, value)

    @staticmethod
    def _lock_file(lock) -> None:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        else:  # pragma: no cover - exercised by Windows builds
            lock.seek(0)
            if not lock.read(1):
                lock.write("0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)

    @staticmethod
    def _unlock_file(lock) -> None:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - exercised by Windows builds
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    @contextmanager
    def _locked(self, *, reload: bool = True) -> Iterator[None]:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with self._mutex:
            with open(self.lock_file, "a+") as lock:
                self._lock_file(lock)
                try:
                    if reload:
                        self.data = self._read_unlocked()
                    yield
                finally:
                    self._unlock_file(lock)

    def _read_unlocked(self) -> Dict[str, Any]:
        if not self.config_file.exists():
            return {}
        with open(self.config_file) as settings_file:
            value = json.load(settings_file)
        if not isinstance(value, dict):
            raise ValueError("settings file root must be an object")
        return value

    def _save_unlocked(self) -> None:
        """Durably replace the complete settings file in the same directory."""
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.config_file.name}.",
            suffix=".tmp",
            dir=self.config_file.parent,
        )
        try:
            with os.fdopen(fd, "w") as temporary:
                json.dump(self.data, temporary, indent=4)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.config_file)
            # POSIX permits fsync on the directory to persist the rename.
            # Windows' ReplaceFile semantics are already used by os.replace,
            # but opening a directory as a file is not supported there.
            if os.name != "nt":
                directory_fd = os.open(self.config_file.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def load(self):
        with self._locked():
            return copy.deepcopy(self.data)

    def save(self):
        # Kept for compatibility with callers which directly update ``data``.
        # Endpoint mutations use reload-under-lock operations below instead.
        with self._locked(reload=False):
            self._save_unlocked()

    @staticmethod
    def _current_revision(data: Dict[str, Any]) -> int:
        """Return the recoverable CAS revision of historical canonical data.

        Documents written before validation may be malformed. A valid
        non-negative integer revision is still authoritative; absent, boolean,
        string, and negative revisions use revision 0 for an explicit validated
        repair, matching the Rust settings endpoint.
        """
        envelope = data.get(RULES_KEY)
        revision = envelope.get("revision") if isinstance(envelope, dict) else None
        return (
            revision
            if isinstance(revision, int)
            and not isinstance(revision, bool)
            and 0 <= revision <= MAX_SAFE_REVISION
            else 0
        )

    @staticmethod
    def _check_expected_revision(data: Dict[str, Any], expected: Optional[int]) -> None:
        if expected is None:
            return
        current = Settings._current_revision(data)
        if expected != current:
            raise RulesConflictError(
                f"rules_v2 revision conflict: expected {expected}, current revision is {current}; "
                "reload or merge before retrying"
            )

    def get(self, key: str, default=None):
        with self._locked():
            has_envelope = RULES_KEY in self.data
            envelope = self.data.get(RULES_KEY)
            if key == RULES_KEY:
                # Return historical malformed documents verbatim so recovery
                # tools can export or explicitly replace them.
                return copy.deepcopy(envelope) if has_envelope else default
            projection = None
            if has_envelope:
                try:
                    projection = compatibility_projection(envelope)
                except RulesValidationError:
                    # A document written by an older/unvalidated implementation
                    # cannot be projected safely. Keep unrelated preferences and
                    # persisted predecessor values readable for recovery.
                    pass
            if not key:
                result = copy.deepcopy(self.data)
                if projection is not None:
                    result.update(projection)
                return result
            if projection is not None and key in LEGACY_RULE_KEYS:
                return copy.deepcopy(projection[key])
            return copy.deepcopy(self.data.get(key, default))

    def set(self, key, value, expected_revision: Optional[int] = None):
        with self._locked():
            has_envelope = RULES_KEY in self.data
            envelope = self.data.get(RULES_KEY)
            if has_envelope and key in LEGACY_RULE_KEYS:
                try:
                    validate_rules_envelope(envelope)
                except RulesValidationError as error:
                    raise RulesConflictError(
                        "legacy rule writes are disabled while rules_v2 requires explicit recovery: "
                        f"{error}"
                    )
                self._check_expected_revision(self.data, expected_revision)
                if envelope["revision"] == MAX_SAFE_REVISION:
                    raise RulesValidationError(
                        f"rules_v2.revision cannot be incremented beyond {MAX_SAFE_REVISION}"
                    )
                updated = translate_legacy_write(envelope, key, value)
                updated["revision"] = envelope["revision"] + 1
                self.data[RULES_KEY] = updated
                self._save_unlocked()
                return copy.deepcopy(compatibility_projection(updated)[key])
            if not has_envelope and key in LEGACY_RULE_KEYS:
                self._check_expected_revision(self.data, expected_revision)
            if key == RULES_KEY:
                raise RulesValidationError(
                    "rules_v2 must be written with the atomic canonical settings operation"
                )
            # Preserve historical arbitrary preference behavior: falsey values
            # remove a key rather than being persisted.
            if value:
                self.data[key] = copy.deepcopy(value)
            else:
                self.data.pop(key, None)
            self._save_unlocked()
            return copy.deepcopy(value)

    def replace_rules(self, proposed: Any) -> Dict[str, Any]:
        validated = validate_rules_envelope(proposed)
        expected = validated["revision"]
        if expected == MAX_SAFE_REVISION:
            raise RulesValidationError(
                f"rules_v2.revision cannot be incremented beyond {MAX_SAFE_REVISION}"
            )
        with self._locked():
            self._check_expected_revision(self.data, expected)
            stored = copy.deepcopy(validated)
            stored["revision"] = expected + 1
            self.data[RULES_KEY] = stored
            self._save_unlocked()
            return copy.deepcopy(stored)
