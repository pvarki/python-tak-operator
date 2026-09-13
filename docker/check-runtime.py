"""Smoke-test the production container and its runtime dependencies."""

import ctypes
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile


class JavaVMOption(ctypes.Structure):
    """Option layout from the JNI invocation API."""

    _fields_ = [("optionString", ctypes.c_char_p), ("extraInfo", ctypes.c_void_p)]


class JavaVMInitArgs(ctypes.Structure):
    """VM initialization layout from the JNI invocation API."""

    _fields_ = [
        ("version", ctypes.c_int),
        ("nOptions", ctypes.c_int),
        ("options", ctypes.POINTER(JavaVMOption)),
        ("ignoreUnrecognized", ctypes.c_ubyte),
    ]


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

    # Exercise JVM startup through JNI.
    jvm = ctypes.CDLL(str(java_home / "lib/server/libjvm.so"))
    jvm.JNI_CreateJavaVM.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(JavaVMInitArgs),
    ]
    jvm.JNI_CreateJavaVM.restype = ctypes.c_int
    options = (JavaVMOption * 1)(JavaVMOption(f"-Djava.class.path={tak_jar}".encode(), None))
    args = JavaVMInitArgs(0x00010008, 1, options, 0)  # JNI_VERSION_1_8
    vm = ctypes.c_void_p()
    environment = ctypes.c_void_p()
    result = jvm.JNI_CreateJavaVM(ctypes.byref(vm), ctypes.byref(environment), ctypes.byref(args))
    assert result == 0, f"JNI_CreateJavaVM failed: {result}"
    # DestroyJavaVM is slot 3 of JNIInvokeInterface, after its three reserved slots.
    functions = ctypes.cast(vm, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    destroy_vm = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(functions[3])
    assert destroy_vm(vm) == 0

    for tool in ("javac", "gcc", "uv", "uvx", "pip", "pip3"):
        assert shutil.which(tool) is None, f"Build tool retained in runtime: {tool}"
    assert not (java_home / "include/jni.h").exists()
    assert importlib.util.find_spec("pip") is None
    assert not (Path(tempfile.gettempdir()) / "wheelhouse").exists()
    print("Python 3.14, Cloudcoil, TAKServer artifacts, JNI JVM startup, and runtime cleanup passed.")


if __name__ == "__main__":
    main()
