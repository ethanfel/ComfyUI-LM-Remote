"""Validated, reloadable configuration for ComfyUI-LM-Remote."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_PACKAGE_DIR = Path(__file__).resolve().parent
_LEGACY_CONFIG_FILE = _PACKAGE_DIR / "config.json"
_CONFIG_DIRECTORY_NAME = "ComfyUI-LM-Remote"
_CONFIG_FILE_NAME = "config.json"
_KNOWN_FIELDS = frozenset({"remote_url", "timeout", "path_mappings"})
_MAX_TIMEOUT = 300
_MAX_MAPPINGS = 100
_MAX_VALUE_LENGTH = 4096
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class ConfigValidationError(ValueError):
    """Raised when a proposed configuration value is invalid."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


class ConfigConflictError(RuntimeError):
    """Raised when a browser attempts to replace a stale configuration."""


@dataclass(frozen=True)
class ConfigSnapshot:
    """One coherent set of effective runtime values."""

    generation: int
    remote_url: str
    timeout: int
    path_mappings: tuple[tuple[str, str], ...]

    def mappings_dict(self) -> dict[str, str]:
        return dict(self.path_mappings)

    def map_path(self, remote_path: str) -> str:
        """Map a path using only the values captured by this snapshot."""
        if not isinstance(remote_path, str):
            return remote_path
        normalized_path = remote_path.replace("\\", "/")
        for remote_prefix, local_prefix in self.path_mappings:
            is_root = remote_prefix == "/"
            if normalized_path == remote_prefix:
                remainder = ""
            elif is_root and normalized_path.startswith("/"):
                remainder = normalized_path[1:]
            elif normalized_path.startswith(f"{remote_prefix}/"):
                remainder = normalized_path[len(remote_prefix) + 1 :]
            else:
                continue

            if not remainder:
                return local_prefix
            separator = (
                "\\" if "\\" in local_prefix and "/" not in local_prefix else os.sep
            )
            local_base = local_prefix.rstrip("/\\")
            return f"{local_base}{separator}{remainder.replace('/', separator)}"
        return remote_path


def _default_user_config_file(environ: Mapping[str, str]) -> Path:
    explicit_path = environ.get("LM_REMOTE_CONFIG", "").strip()
    if explicit_path:
        return Path(explicit_path).expanduser()

    try:
        import folder_paths  # type: ignore

        user_directory = Path(folder_paths.get_user_directory())
        return user_directory / _CONFIG_DIRECTORY_NAME / _CONFIG_FILE_NAME
    except Exception:
        # Outside ComfyUI (for example, documentation tools), preserve the
        # historical package-level behaviour.
        return _LEGACY_CONFIG_FILE


def _normalize_url(value: object, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ConfigValidationError("remote_url", "Remote URL must be text.")
    value = value.strip()
    if not value:
        if allow_empty:
            return ""
        raise ConfigValidationError("remote_url", "Enter a remote LoRA Manager URL.")
    if _CONTROL_CHARACTERS.search(value):
        raise ConfigValidationError(
            "remote_url", "Remote URL contains invalid characters."
        )

    try:
        parsed = urlsplit(value)
        # Accessing .port performs its own range and syntax validation.
        parsed.port
    except ValueError as exc:
        raise ConfigValidationError(
            "remote_url", "Remote URL has an invalid port."
        ) from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ConfigValidationError(
            "remote_url", "Remote URL must use http:// or https://."
        )
    if not parsed.hostname:
        raise ConfigValidationError(
            "remote_url", "Remote URL must include a host name."
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConfigValidationError(
            "remote_url", "Credentials are not allowed in the remote URL."
        )
    if parsed.query or parsed.fragment:
        raise ConfigValidationError(
            "remote_url", "Remote URL cannot contain a query or fragment."
        )

    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def _normalize_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError("timeout", "Timeout must be a whole number.")
    if value < 1 or value > _MAX_TIMEOUT:
        raise ConfigValidationError(
            "timeout", f"Timeout must be between 1 and {_MAX_TIMEOUT} seconds."
        )
    return value


def _normalize_remote_prefix(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized


def _normalize_mapping_value(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise ConfigValidationError("path_mappings", f"{field} path must be text.")
    value = value.strip()
    if not value:
        raise ConfigValidationError("path_mappings", f"{field} path cannot be empty.")
    if len(value) > _MAX_VALUE_LENGTH:
        raise ConfigValidationError("path_mappings", f"{field} path is too long.")
    if _CONTROL_CHARACTERS.search(value):
        raise ConfigValidationError(
            "path_mappings", f"{field} path contains invalid characters."
        )
    return value


def _normalize_mappings(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        raise ConfigValidationError("path_mappings", "Path mappings must be an object.")
    if len(value) > _MAX_MAPPINGS:
        raise ConfigValidationError(
            "path_mappings", f"No more than {_MAX_MAPPINGS} path mappings are allowed."
        )

    normalized: dict[str, str] = {}
    for remote_value, local_value in value.items():
        remote_prefix = _normalize_remote_prefix(
            _normalize_mapping_value("Remote", remote_value)
        )
        if not remote_prefix:
            raise ConfigValidationError(
                "path_mappings",
                "Remote path cannot consist only of path separators.",
            )
        local_prefix = _normalize_mapping_value("Local", local_value)
        if remote_prefix in normalized:
            raise ConfigValidationError(
                "path_mappings", f"Duplicate remote path prefix: {remote_prefix}"
            )
        normalized[remote_prefix] = local_prefix

    # Specific mappings must win over broader parent mappings.
    return tuple(
        sorted(normalized.items(), key=lambda pair: len(pair[0]), reverse=True)
    )


def validate_config(data: object, *, allow_empty_url: bool = True) -> dict[str, object]:
    """Validate and normalize a complete stored configuration."""
    if not isinstance(data, dict):
        raise ConfigValidationError("config", "Configuration must be a JSON object.")
    unknown = set(data) - _KNOWN_FIELDS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigValidationError(
            "config", f"Unknown configuration field(s): {names}"
        )

    return {
        "remote_url": _normalize_url(
            data.get("remote_url", ""), allow_empty=allow_empty_url
        ),
        "timeout": _normalize_timeout(data.get("timeout", 30)),
        "path_mappings": dict(_normalize_mappings(data.get("path_mappings", {}))),
    }


def _revision_for(data: Mapping[str, object]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _revision_for_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


class RemoteConfig:
    """Thread-safe configuration with legacy fallback and atomic persistence."""

    def __init__(
        self,
        config_file: str | Path | None = None,
        legacy_config_file: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ = environ if environ is not None else os.environ
        self._explicit_target = config_file is None and bool(
            self._environ.get("LM_REMOTE_CONFIG", "").strip()
        )
        self._config_file = (
            Path(config_file)
            if config_file
            else _default_user_config_file(self._environ)
        )
        self._legacy_config_file = (
            Path(legacy_config_file) if legacy_config_file else _LEGACY_CONFIG_FILE
        )
        self._lock = threading.RLock()
        self._snapshot = ConfigSnapshot(0, "", 30, ())
        self._configured: dict[str, object] = {
            "remote_url": "",
            "timeout": 30,
            "path_mappings": {},
        }
        self._overrides: dict[str, str | None] = {
            "remote_url": None,
            "timeout": None,
        }
        self._revision = _revision_for({})
        self._source = "defaults"
        self._warnings: list[str] = []
        self.reload()

    def _read_source(self) -> tuple[dict[str, object], str]:
        if self._config_file.exists():
            path = self._config_file
            source = "explicit" if self._explicit_target else "user"
        elif self._explicit_target:
            return {}, "explicit"
        elif self._legacy_config_file.exists():
            path = self._legacy_config_file
            source = "legacy"
        else:
            return {}, "defaults"

        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ConfigValidationError(
                "config", "Configuration file must contain an object."
            )
        return data, source

    def _source_location(self) -> tuple[Path | None, str]:
        if self._config_file.exists():
            source = "explicit" if self._explicit_target else "user"
            return self._config_file, source
        if self._explicit_target:
            return None, "explicit"
        if self._legacy_config_file.exists():
            return self._legacy_config_file, "legacy"
        return None, "defaults"

    def _effective_values(
        self, configured: dict[str, object]
    ) -> tuple[dict[str, object], dict[str, str | None], list[str]]:
        effective = {
            "remote_url": configured["remote_url"],
            "timeout": configured["timeout"],
            "path_mappings": dict(configured["path_mappings"]),
        }
        overrides: dict[str, str | None] = {"remote_url": None, "timeout": None}
        warnings: list[str] = []

        env_url = self._environ.get("LM_REMOTE_URL", "")
        if env_url:
            try:
                effective["remote_url"] = _normalize_url(env_url)
                overrides["remote_url"] = "LM_REMOTE_URL"
            except ConfigValidationError as exc:
                warnings.append(f"Ignoring invalid LM_REMOTE_URL: {exc}")

        env_timeout = self._environ.get("LM_REMOTE_TIMEOUT", "")
        if env_timeout:
            try:
                if isinstance(env_timeout, str) and env_timeout.strip().isdigit():
                    parsed_timeout: object = int(env_timeout.strip())
                else:
                    parsed_timeout = env_timeout
                effective["timeout"] = _normalize_timeout(parsed_timeout)
                overrides["timeout"] = "LM_REMOTE_TIMEOUT"
            except ConfigValidationError as exc:
                warnings.append(f"Ignoring invalid LM_REMOTE_TIMEOUT: {exc}")

        return effective, overrides, warnings

    def reload(self) -> ConfigSnapshot:
        """Reload persisted and environment-managed values without partial mutation."""
        with self._lock:
            warnings: list[str] = []
            try:
                raw, source = self._read_source()
                known_values = {key: raw[key] for key in _KNOWN_FIELDS if key in raw}
                configured = validate_config(known_values)
                revision = _revision_for(raw)
            except (OSError, json.JSONDecodeError, ConfigValidationError) as exc:
                logger.warning("[LM-Remote] Failed to read configuration: %s", exc)
                source_path, source = self._source_location()
                raw = {}
                configured = validate_config({})
                try:
                    revision = (
                        _revision_for_file(source_path)
                        if source_path is not None
                        else _revision_for(raw)
                    )
                except OSError:
                    revision = _revision_for(raw)
                warnings.append(f"Stored configuration could not be loaded: {exc}")

            effective, overrides, env_warnings = self._effective_values(configured)
            warnings.extend(env_warnings)
            generation = self._snapshot.generation + 1
            snapshot = ConfigSnapshot(
                generation=generation,
                remote_url=str(effective["remote_url"]),
                timeout=int(effective["timeout"]),
                path_mappings=_normalize_mappings(effective["path_mappings"]),
            )
            self._configured = configured
            self._overrides = overrides
            self._revision = revision
            self._source = source
            self._warnings = warnings
            self._snapshot = snapshot
            return snapshot

    @property
    def snapshot(self) -> ConfigSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def generation(self) -> int:
        return self.snapshot.generation

    @property
    def remote_url(self) -> str:
        return self.snapshot.remote_url

    @property
    def timeout(self) -> int:
        return self.snapshot.timeout

    @property
    def path_mappings(self) -> dict[str, str]:
        return self.snapshot.mappings_dict()

    @property
    def is_configured(self) -> bool:
        return bool(self.snapshot.remote_url)

    def as_dict(self) -> dict[str, object]:
        """Return browser-safe configured/effective values and source metadata."""
        with self._lock:
            snapshot = self._snapshot
            return {
                "configured": {
                    "remote_url": self._configured["remote_url"],
                    "timeout": self._configured["timeout"],
                    "path_mappings": dict(self._configured["path_mappings"]),
                },
                "effective": {
                    "remote_url": snapshot.remote_url,
                    "timeout": snapshot.timeout,
                    "path_mappings": snapshot.mappings_dict(),
                },
                "overrides": dict(self._overrides),
                "revision": self._revision,
                "generation": snapshot.generation,
                "storage": {
                    "source": self._source,
                    "writable": self._storage_writable(),
                },
                "warnings": list(self._warnings),
                "restart_required": False,
            }

    def _storage_writable(self) -> bool:
        """Return whether an atomic write can be created beside the target."""
        target = self._config_file
        try:
            if target.exists() and target.is_dir():
                return False

            ancestor = target.parent
            while not ancestor.exists():
                parent = ancestor.parent
                if parent == ancestor:
                    return False
                ancestor = parent
            if not ancestor.is_dir():
                return False

            mode = ancestor.stat().st_mode
            write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if not mode & write_bits or not mode & execute_bits:
                return False
            return os.access(ancestor, os.W_OK | os.X_OK)
        except OSError:
            return False

    def save(
        self, data: object, *, expected_revision: str | None = None
    ) -> ConfigSnapshot:
        """Atomically persist a complete configuration and activate it."""
        normalized = validate_config(data)
        serializable = {
            "remote_url": normalized["remote_url"],
            "timeout": normalized["timeout"],
            "path_mappings": normalized["path_mappings"],
        }

        with self._lock:
            try:
                current_raw, _ = self._read_source()
            except (OSError, json.JSONDecodeError, ConfigValidationError):
                source_path, _ = self._source_location()
                try:
                    current_revision = (
                        _revision_for_file(source_path)
                        if source_path is not None
                        else _revision_for({})
                    )
                except OSError as revision_exc:
                    raise ConfigConflictError(
                        "The stored configuration changed and can no longer be read."
                    ) from revision_exc
                current_raw = {}
            else:
                current_revision = _revision_for(current_raw)
            if expected_revision is not None and expected_revision != current_revision:
                raise ConfigConflictError(
                    "The configuration changed in another window. Reload it before saving."
                )

            # Preserve future/third-party keys from an existing file while replacing
            # only fields owned by LM Remote.
            output = dict(current_raw)
            output.update(serializable)
            self._write_atomic(output)
            return self.reload()

    def _write_atomic(self, data: Mapping[str, object]) -> None:
        target = self._config_file
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_mode = (
            stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, existing_mode)
            os.replace(temporary_path, target)
            temporary_path = None
            try:
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Directory fsync is not supported by every filesystem.
                pass
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def map_path(self, remote_path: str) -> str:
        """Apply the longest boundary-aware remote-to-local path mapping."""
        return self.snapshot.map_path(remote_path)


remote_config = RemoteConfig()
