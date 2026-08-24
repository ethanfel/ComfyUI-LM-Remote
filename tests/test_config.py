from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def config_module():
    module_name = "lm_remote_config_test_module"
    module_path = Path(__file__).resolve().parents[1] / "config.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(module_name, None)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_legacy_config_loads_and_first_save_migrates(config_module, tmp_path):
    legacy = tmp_path / "package" / "config.json"
    user = tmp_path / "user" / "config.json"
    original = {
        "remote_url": "http://legacy.local:8188",
        "timeout": 30,
        "path_mappings": {},
        "future_setting": {"preserve": True},
    }
    write_json(legacy, original)

    config = config_module.RemoteConfig(user, legacy, environ={})
    state = config.as_dict()
    assert state["storage"]["source"] == "legacy"
    assert state["effective"]["remote_url"] == "http://legacy.local:8188"

    config.save(
        {
            "remote_url": "http://new.local:8188/",
            "timeout": 45,
            "path_mappings": {"/remote": "/local"},
        },
        expected_revision=state["revision"],
    )

    assert json.loads(legacy.read_text(encoding="utf-8")) == original
    saved = json.loads(user.read_text(encoding="utf-8"))
    assert saved["remote_url"] == "http://new.local:8188"
    assert saved["future_setting"] == {"preserve": True}
    assert config.as_dict()["storage"]["source"] == "user"


def test_environment_values_override_without_rewriting_stored_values(
    config_module, tmp_path
):
    user = tmp_path / "config.json"
    write_json(
        user,
        {
            "remote_url": "http://stored.local:8188",
            "timeout": 30,
            "path_mappings": {},
        },
    )
    config = config_module.RemoteConfig(
        user,
        tmp_path / "missing.json",
        environ={
            "LM_REMOTE_URL": "https://managed.local:443/",
            "LM_REMOTE_TIMEOUT": "12",
        },
    )

    state = config.as_dict()
    assert state["configured"]["remote_url"] == "http://stored.local:8188"
    assert state["effective"]["remote_url"] == "https://managed.local:443"
    assert state["effective"]["timeout"] == 12
    assert state["overrides"] == {
        "remote_url": "LM_REMOTE_URL",
        "timeout": "LM_REMOTE_TIMEOUT",
    }


def test_explicit_config_environment_reports_explicit_source(config_module, tmp_path):
    explicit = tmp_path / "managed" / "remote.json"
    config = config_module.RemoteConfig(
        legacy_config_file=tmp_path / "missing-legacy.json",
        environ={"LM_REMOTE_CONFIG": str(explicit)},
    )
    initial = config.as_dict()
    config.save(
        {"remote_url": "http://manager.local", "timeout": 30, "path_mappings": {}},
        expected_revision=initial["revision"],
    )

    assert config.as_dict()["storage"]["source"] == "explicit"


def test_explicit_missing_target_does_not_fall_back_to_legacy(config_module, tmp_path):
    explicit = tmp_path / "managed" / "remote.json"
    legacy = tmp_path / "package" / "config.json"
    legacy_data = {
        "remote_url": "http://legacy.local",
        "timeout": 30,
        "path_mappings": {},
    }
    write_json(legacy, legacy_data)

    config = config_module.RemoteConfig(
        legacy_config_file=legacy,
        environ={"LM_REMOTE_CONFIG": str(explicit)},
    )
    state = config.as_dict()

    assert state["storage"]["source"] == "explicit"
    assert state["configured"]["remote_url"] == ""
    config.save(
        {"remote_url": "http://explicit.local", "timeout": 30, "path_mappings": {}},
        expected_revision=state["revision"],
    )
    assert json.loads(legacy.read_text(encoding="utf-8")) == legacy_data
    assert (
        json.loads(explicit.read_text(encoding="utf-8"))["remote_url"]
        == "http://explicit.local"
    )


def test_invalid_environment_timeout_is_ignored(config_module, tmp_path):
    config = config_module.RemoteConfig(
        tmp_path / "missing-user.json",
        tmp_path / "missing-legacy.json",
        environ={"LM_REMOTE_TIMEOUT": "not-a-number"},
    )
    state = config.as_dict()
    assert state["effective"]["timeout"] == 30
    assert state["overrides"]["timeout"] is None
    assert any("LM_REMOTE_TIMEOUT" in warning for warning in state["warnings"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("remote_url", "ftp://manager.local"),
        ("remote_url", "http://user:secret@manager.local"),
        ("remote_url", "http://manager.local?query=yes"),
        ("timeout", True),
        ("timeout", 0),
        ("timeout", 301),
        ("path_mappings", []),
    ],
)
def test_validation_rejects_invalid_values(config_module, field, value):
    candidate = {
        "remote_url": "http://manager.local",
        "timeout": 30,
        "path_mappings": {},
    }
    candidate[field] = value
    with pytest.raises(config_module.ConfigValidationError) as caught:
        config_module.validate_config(candidate)
    assert caught.value.field == field


def test_mapping_is_longest_first_and_path_boundary_aware(config_module, tmp_path):
    user = tmp_path / "config.json"
    write_json(
        user,
        {
            "remote_url": "http://manager.local",
            "timeout": 30,
            "path_mappings": {
                "/models": "/mnt/general",
                "/models/special": "/mnt/special",
            },
        },
    )
    config = config_module.RemoteConfig(user, tmp_path / "missing.json", environ={})
    assert (
        config.map_path("/models/special/a.safetensors") == "/mnt/special/a.safetensors"
    )
    assert (
        config.map_path("/models/base.safetensors") == "/mnt/general/base.safetensors"
    )
    assert config.map_path("/models-old/a.safetensors") == "/models-old/a.safetensors"


def test_mapping_rejects_separator_only_prefix_but_allows_root(config_module):
    candidate = {
        "remote_url": "http://manager.local",
        "timeout": 30,
        "path_mappings": {"////": "/mnt/invalid"},
    }
    with pytest.raises(config_module.ConfigValidationError) as caught:
        config_module.validate_config(candidate)
    assert caught.value.field == "path_mappings"

    root_mapping = config_module.validate_config(
        {**candidate, "path_mappings": {"/": "/mnt/root"}}
    )
    snapshot = config_module.ConfigSnapshot(
        1,
        root_mapping["remote_url"],
        root_mapping["timeout"],
        config_module._normalize_mappings(root_mapping["path_mappings"]),
    )
    assert (
        snapshot.map_path("/models/a.safetensors") == "/mnt/root/models/a.safetensors"
    )


def test_snapshot_mapping_does_not_change_after_reload(config_module, tmp_path):
    user = tmp_path / "config.json"
    write_json(
        user,
        {
            "remote_url": "http://one.local",
            "timeout": 30,
            "path_mappings": {"/remote": "/local-one"},
        },
    )
    config = config_module.RemoteConfig(user, tmp_path / "missing.json", environ={})
    original = config.snapshot
    revision = config.as_dict()["revision"]

    config.save(
        {
            "remote_url": "http://two.local",
            "timeout": 30,
            "path_mappings": {"/remote": "/local-two"},
        },
        expected_revision=revision,
    )

    assert original.map_path("/remote/a.safetensors") == "/local-one/a.safetensors"
    assert config.map_path("/remote/a.safetensors") == "/local-two/a.safetensors"


def test_storage_reports_unwritable_when_parent_is_not_a_directory(
    config_module, tmp_path
):
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("blocked", encoding="utf-8")
    config = config_module.RemoteConfig(
        blocking_file / "config.json",
        tmp_path / "missing-legacy.json",
        environ={},
    )

    assert config.as_dict()["storage"]["writable"] is False


def test_stale_revision_does_not_overwrite_external_change(config_module, tmp_path):
    user = tmp_path / "config.json"
    initial = {"remote_url": "http://one.local", "timeout": 30, "path_mappings": {}}
    write_json(user, initial)
    config = config_module.RemoteConfig(user, tmp_path / "missing.json", environ={})
    revision = config.as_dict()["revision"]
    write_json(
        user,
        {"remote_url": "http://two.local", "timeout": 30, "path_mappings": {}},
    )

    with pytest.raises(config_module.ConfigConflictError):
        config.save(initial, expected_revision=revision)
    assert (
        json.loads(user.read_text(encoding="utf-8"))["remote_url"] == "http://two.local"
    )


def test_atomic_write_failure_preserves_file_and_live_snapshot(
    config_module, tmp_path, monkeypatch
):
    user = tmp_path / "config.json"
    initial = {"remote_url": "http://one.local", "timeout": 30, "path_mappings": {}}
    write_json(user, initial)
    config = config_module.RemoteConfig(user, tmp_path / "missing.json", environ={})
    before = config.snapshot
    revision = config.as_dict()["revision"]

    def fail_replace(source, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        config.save(
            {"remote_url": "http://two.local", "timeout": 50, "path_mappings": {}},
            expected_revision=revision,
        )

    assert json.loads(user.read_text(encoding="utf-8")) == initial
    assert config.snapshot == before
    assert not list(tmp_path.glob(".config.json.*.tmp"))


def test_invalid_json_can_be_repaired_from_loaded_revision(config_module, tmp_path):
    user = tmp_path / "config.json"
    user.write_text("{not valid", encoding="utf-8")
    config = config_module.RemoteConfig(user, tmp_path / "missing.json", environ={})
    state = config.as_dict()
    assert state["effective"]["remote_url"] == ""
    assert state["warnings"]

    config.save(
        {"remote_url": "http://fixed.local", "timeout": 30, "path_mappings": {}},
        expected_revision=state["revision"],
    )
    assert (
        json.loads(user.read_text(encoding="utf-8"))["remote_url"]
        == "http://fixed.local"
    )
