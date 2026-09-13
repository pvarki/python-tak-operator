"""Exercise JNI semantics using a Java-shaped facade, without starting Java."""

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from takoperator.bridge import FileAuthUserState, JniFileAuthStore, TakMutationError
from takoperator.runtime import TakJvmRuntime, TakJvmSettings


def java_user(identifier: str = "alice", *, groups: tuple[str, ...] = ()) -> MagicMock:
    """A JAXB-shaped object with independently mutable list and scalar fields."""
    user = MagicMock()
    for field, initial in (
        ("Identifier", identifier),
        ("Fingerprint", None),
        ("Password", "hashed-credential"),
        ("Role", None),
        ("PasswordHashed", True),
    ):
        getter = getattr(user, "isPasswordHashed" if field == "PasswordHashed" else "get" + field)
        getter.return_value = initial

        def set_value(value: Any, target: Any = getter) -> None:
            target.return_value = value

        getattr(user, "set" + field).side_effect = set_value
    for field in ("GroupList", "GroupListIN", "GroupListOUT"):
        values = list(groups if field == "GroupList" else ())
        collection = getattr(user, "get" + field).return_value
        collection.toArray.side_effect = lambda target=values: list(target)
        collection.add.side_effect = values.append
        collection.clear.side_effect = values.clear
    return user


@pytest.fixture
def facade() -> tuple[JniFileAuthStore, MagicMock, dict[str, Any]]:
    """Provide persisted objects separately from replacement snapshots."""
    manager = MagicMock()
    users: dict[str, Any] = {}
    manager.getFirstUser.side_effect = users.get
    manager.getAllUsers.return_value.toArray.side_effect = lambda: list(users.values())
    manager.getUserRole.return_value.value.return_value = "ROLE_ANONYMOUS"
    ssl = MagicMock()
    ssl.getCertificateUserName.return_value = "alice"
    ssl.loadCertFingerprintForEndUser.return_value = "AB" * 32
    store = JniFileAuthStore(manager, user_class=java_user, role_class=MagicMock(), boolean_class=bool, ssl_helper=ssl)
    return store, manager, users


def test_directional_snapshot_and_absent_role(facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]]) -> None:
    store, _, users = facade
    users["alice"] = java_user(groups=("ordinary",))
    users["alice"].getGroupListIN().add("incoming")
    users["alice"].getGroupListOUT().add("outgoing")
    expected = FileAuthUserState(
        "alice", groups=frozenset({"ordinary"}), in_groups=frozenset({"incoming"}), out_groups=frozenset({"outgoing"})
    )
    assert store.get_user("alice") == expected
    assert store.list_users() == (expected,)
    assert store.get_user("missing") is None
    assert "hashed-credential" not in repr(expected)


def test_replacement_preserves_credentials_and_live_object(
    facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]],
) -> None:
    store, manager, users = facade
    original = java_user(groups=("old",))
    users["alice"] = original

    def save(user: Any, _hashed: bool, _old: Any) -> None:
        users["alice"] = user

    manager.addOrUpdateUser.side_effect = save
    desired = FileAuthUserState("alice", fingerprint="AB" * 32, in_groups=frozenset({"new"}))
    assert store.replace_existing_user_state(desired) == desired
    replacement, hashed, old = manager.addOrUpdateUser.call_args.args
    assert replacement is not original and old is not original and replacement is not old
    assert original.getGroupList().toArray() == ["old"]
    assert old.getGroupList().toArray() == ["old"]
    assert replacement.getPassword() == old.getPassword() == "hashed-credential"
    assert replacement.isPasswordHashed() is True and hashed is True
    assert replacement.getRole() is None
    store.replace_existing_user_state(desired)
    assert manager.addOrUpdateUser.call_count == 1


def test_create_rereads_and_retry_does_not_reset_credentials(
    facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]],
) -> None:
    store, manager, users = facade

    def save(identifier: str, _password: str | None, _hashed: bool) -> None:
        users[identifier] = java_user(identifier)

    manager.addOrUpdateUser.side_effect = save
    assert store.create_password_user("alice", "initial").identifier == "alice"
    store.create_password_user("alice", "different")
    assert manager.addOrUpdateUser.call_count == 1
    store.update_password("alice", "changed")
    assert manager.addOrUpdateUser.call_count == 2
    with pytest.raises(ValueError, match="does not exist"):
        store.update_password("missing", "changed")


def test_partial_failure_exposes_observation(facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]]) -> None:
    store, manager, users = facade

    def persist_then_fail(*_args: Any) -> None:
        users["alice"] = java_user()
        raise RuntimeError("lost reply")

    manager.addOrUpdateUser.side_effect = persist_then_fail
    with pytest.raises(TakMutationError) as error:
        store.create_password_user("alice", None)
    assert error.value.observation_available
    assert error.value.observed == FileAuthUserState("alice")


def test_delete_checks_observed_absence(facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]]) -> None:
    store, manager, users = facade
    users["alice"] = java_user()
    with pytest.raises(RuntimeError, match="remains"):
        store.delete_user("alice")
    manager.removeUser.side_effect = users.pop
    store.delete_user("alice")
    store.delete_user("alice")
    assert manager.removeUser.call_count == 2


def test_certificate_validates_before_mutation(
    facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]],
    tmp_path: Path,
) -> None:
    store, manager, users = facade
    certificate = tmp_path / "certificate.pem"
    assert store.validate_certificate(certificate, "alice") == "AB" * 32
    with pytest.raises(ValueError, match="identity"):
        store.create_certificate_user(certificate, "mallory")
    manager.addOrUpdateUserFromCertificate.assert_not_called()

    def register(_certificate: Any) -> None:
        users["alice"] = java_user()
        users["alice"].setFingerprint("AB" * 32)

    manager.addOrUpdateUserFromCertificate.side_effect = register
    assert store.create_certificate_user(certificate, "alice").fingerprint == "AB" * 32
    store.create_certificate_user(certificate, "alice")
    manager.addOrUpdateUserFromCertificate.assert_called_once()


def test_store_rejects_fork(facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]], monkeypatch: Any) -> None:
    store, _, _ = facade
    monkeypatch.setattr("takoperator.bridge.os.getpid", lambda: -1)
    with pytest.raises(RuntimeError, match="forking"):
        store.get_user("alice")


def test_runtime_config_separates_discovery_from_local_bind(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TAK_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("TAK_IGNITE_HOST", "tak-ignite")
    monkeypatch.setenv("TAK_IGNITE_BIND_ADDRESS", "10.1.0.99")
    settings = TakJvmSettings.from_env()
    config = settings.write_configuration()
    assert config.read_bytes().startswith(b"<?xml")
    ignite = (tmp_path / "data/TAKIgniteConfig.xml").read_text()
    assert 'igniteHost="10.1.0.99"' in ignite
    assert 'igniteMulticast="false"' in ignite
    assert "tak-ignite" not in ignite
    assert "-Dcom.bbn.marti.takcl.igniteIpAddressOverride=tak-ignite" in settings.options(config)
    assert (tmp_path / "temporary").is_dir()
    assert (tmp_path / "fallback").is_dir()


def test_runtime_checks_missing_jar_and_close(tmp_path: Path) -> None:
    settings = replace(TakJvmSettings(), jar=tmp_path / "missing.jar")
    runtime = TakJvmRuntime(settings)
    with pytest.raises(FileNotFoundError):
        runtime.start()
    runtime.close()
    runtime.close()
    with pytest.raises(RuntimeError, match="closing"):
        runtime.start()


def test_runtime_bootstraps_once_and_closes_its_own_client(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("IGNITE_WORK_DIR", raising=False)
    jar = tmp_path / "UserManager.jar"
    jar.touch()
    settings = TakJvmSettings(jar=jar, directory=tmp_path)
    config = MagicMock(vm_running=False)
    java = MagicMock()
    classes: dict[str, Any] = {}

    def load(name: str) -> Any:
        return classes.setdefault(name, MagicMock())

    java.autoclass.side_effect = load
    modules = {"jnius_config": config, "jnius": java}
    monkeypatch.setattr("takoperator.runtime.importlib.import_module", modules.__getitem__)
    monkeypatch.setattr(TakJvmRuntime, "_owner", None)
    runtime = TakJvmRuntime(settings)
    store = runtime.start()
    assert os.environ["IGNITE_WORK_DIR"] == str(tmp_path / "temporary")
    assert runtime.start() is store
    config.set_classpath.assert_called_once_with(str(jar))
    config.add_options.assert_called_once()
    helper = classes["com.bbn.marti.takcl.TakclIgniteHelper"]
    helper.getUserManager.assert_called_once()
    with pytest.raises(RuntimeError, match="Only one"):
        TakJvmRuntime(settings).start()
    runtime.close()
    runtime.close()
    helper.closeAssociatedIgniteInstance.assert_called_once_with(helper.getUserManager.call_args.args[0])


def test_runtime_rejects_preexisting_jvm(tmp_path: Path, monkeypatch: Any) -> None:
    jar = tmp_path / "UserManager.jar"
    jar.touch()
    config = MagicMock(vm_running=True)

    def load(_name: str) -> Any:
        return config

    monkeypatch.setattr("takoperator.runtime.importlib.import_module", load)
    monkeypatch.setattr(TakJvmRuntime, "_owner", None)
    with pytest.raises(RuntimeError, match="before importing"):
        TakJvmRuntime(TakJvmSettings(jar=jar, directory=tmp_path)).start()
    config.add_options.assert_not_called()


def test_failed_write_and_read_is_not_reported_as_absence(
    facade: tuple[JniFileAuthStore, MagicMock, dict[str, Any]],
) -> None:
    store, manager, users = facade
    users["alice"] = java_user()
    manager.getFirstUser.side_effect = [users["alice"], RuntimeError("disconnected")]
    manager.removeUser.side_effect = RuntimeError("lost connection")
    with pytest.raises(TakMutationError) as error:
        store.delete_user("alice")
    assert error.value.observation_available is False
