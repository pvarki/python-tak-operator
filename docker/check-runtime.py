"""Smoke-test the production container and its runtime dependencies."""

import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile


def main() -> None:
    """Check the runtime ABI, TAK artifacts, embedded JVM, and build-tool removal."""
    assert sys.version_info[:2] == (3, 14), sys.version
    assert sys.prefix == "/opt/venv", sys.prefix
    for module_name in ("cloudcoil.application", "cloudcoil.models.kubernetes.core.v1", "uvicorn"):
        importlib.import_module(module_name)
    java_home = Path(os.environ["JAVA_HOME"])
    tak_jar = Path("/opt/tak/utils/UserManager.jar")
    with zipfile.ZipFile(tak_jar) as archive:
        assert any(name.endswith(".class") for name in archive.namelist())
    assert Path("/opt/tak/version.txt").read_text().strip()

    # Use the same native binding as reconciliation, without joining an Ignite cluster.
    config = importlib.import_module("jnius_config")
    config.set_classpath(str(tak_jar))
    jnius = importlib.import_module("jnius")
    user_class = jnius.autoclass("com.bbn.marti.xml.bindings.UserAuthenticationFile$User")
    user = user_class()
    user.setIdentifier("runtime-check")
    assert user.getIdentifier() == "runtime-check"
    assert jnius.autoclass("java.lang.System").getProperty("java.specification.version") == "17"

    for tool in ("javac", "gcc", "uv", "uvx", "pip", "pip3"):
        assert shutil.which(tool) is None, f"Build tool retained in runtime: {tool}"
    assert not (java_home / "include/jni.h").exists()
    assert importlib.util.find_spec("pip") is None
    assert not (Path(tempfile.gettempdir()) / "wheelhouse").exists()
    print("Python 3.14, Cloudcoil, TAKServer artifacts, PyJNIus JVM startup, and runtime cleanup passed.")


if __name__ == "__main__":
    main()
