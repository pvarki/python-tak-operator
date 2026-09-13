"""One embedded Java 17 JVM and Ignite client for the operator process."""

import importlib
import os
import tempfile

# Only constructs trusted config XML; never parses XML.
import xml.etree.ElementTree as ET  # nosec B405
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar

from takoperator.bridge import JniFileAuthStore

_JAVA_OPENS = (
    "java.base/java.io",
    "java.base/java.lang",
    "java.base/java.lang.invoke",
    "java.base/java.lang.ref",
    "java.base/java.lang.reflect",
    "java.base/java.math",
    "java.base/java.net",
    "java.base/java.nio",
    "java.base/java.security",
    "java.base/java.security.cert",
    "java.base/java.util",
    "java.base/java.util.concurrent",
    "java.base/java.util.concurrent.atomic",
    "java.base/java.util.concurrent.locks",
    "java.base/javax.security.auth.x500",
    "java.base/jdk.internal.misc",
    "java.base/sun.nio.ch",
    "java.base/sun.reflect.generics.reflectiveObjects",
    "java.base/sun.security.pkcs",
    "java.base/sun.security.pkcs10",
    "java.base/sun.security.pkcs12",
    "java.base/sun.security.provider",
    "java.base/sun.security.rsa",
    "java.base/sun.security.ssl",
    "java.base/sun.security.tools.keytool",
    "java.base/sun.security.util",
    "java.base/sun.security.x509",
    "java.management/com.sun.jmx.mbeanserver",
    "java.sql/java.sql",
    "jdk.management/com.sun.management.internal",
    "jdk.unsupported/sun.misc",
)


@dataclass(frozen=True)
class TakJvmSettings:
    """Discovery seed and local bind address are deliberately independent."""

    jar: Path = Path("/opt/tak/utils/UserManager.jar")
    directory: Path = Path(tempfile.gettempdir()) / "takoperator"
    host: str = "127.0.0.1"
    bind_address: str = "127.0.0.1"
    max_heap: str = "256m"
    processors: int = 2

    @classmethod
    def from_env(cls) -> "TakJvmSettings":
        """Load container configuration without initializing Java."""
        return cls(
            jar=Path(os.environ.get("TAK_USER_MANAGER_JAR", str(cls.jar))),
            directory=Path(os.environ.get("TAK_RUNTIME_DIR", str(cls.directory))),
            host=os.environ.get("TAK_IGNITE_HOST", cls.host),
            bind_address=os.environ.get("TAK_IGNITE_BIND_ADDRESS", cls.bind_address),
            max_heap=os.environ.get("TAK_JVM_MAX_HEAP", cls.max_heap),
            processors=int(os.environ.get("TAK_JVM_PROCESSORS", cls.processors)),
        )

    def write_configuration(self) -> Path:
        """Write independent client configuration; no shared server PVC needed."""
        directory = self.directory.resolve()
        for name in ("temporary", "fallback", "certs", "data"):
            (directory / name).mkdir(parents=True, exist_ok=True)
        common = "http://bbn.com/marti/takcl/config/common"
        root = ET.Element("TAKCLConfiguration", {"xmlns": "http://bbn.com/marti/takcl/config"})
        ET.SubElement(root, f"{{{common}}}TemporaryDirectory").text = str(directory / "temporary")
        ET.SubElement(root, f"{{{common}}}FallbackTemporaryDirectory").text = str(directory / "fallback")
        ET.SubElement(
            root,
            f"{{{common}}}RunnableTAKServerConfig",
            {
                "modelServerDir": str(directory),
                "serverFarmDir": str(directory),
                "jarName": "takserver-core.jar",
                "TAKIgniteConfigFile": "data/TAKIgniteConfig.xml",
                "certificateDirectory": str(directory / "certs"),
                "certToolDirectory": str(directory / "certs"),
            },
        )
        path = directory / "TAKCLConfig.xml"
        ET.ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)
        ignite = ET.Element(
            "TAKIgniteConfiguration",
            {
                "xmlns": "http://bbn.com/marti/xml/config",
                "igniteHost": self.bind_address,
                "cacheOffHeapInitialSizeBytes": "16777216",
                "cacheOffHeapMaxSizeBytes": "67108864",
                "ignitePoolSize": "2",
                "ignitePoolSizeMultiplier": "1",
                "selectorsCount": "1",
                "igniteExplicitSpiConnectionsPerNode": "1",
                "metricsLogFrequency": "0",
            },
        )
        ET.ElementTree(ignite).write(directory / "data/TAKIgniteConfig.xml", encoding="UTF-8", xml_declaration=True)
        return path

    def options(self, configuration: Path) -> tuple[str, ...]:
        """Set options before PyJNIus loads its native extension."""
        directory = self.directory.resolve()
        return (
            f"-Xmx{self.max_heap}",
            f"-XX:ActiveProcessorCount={self.processors}",
            "-Djava.net.preferIPv4Stack=true",
            "-DIGNITE_UPDATE_NOTIFIER=false",
            "-DIGNITE_QUIET=true",
            f"-Dcom.bbn.marti.takcl.config.filepath={configuration}",
            f"-Dcom.bbn.marti.takcl.takIgniteConfigPath={directory / 'data/TAKIgniteConfig.xml'}",
            "-Dcom.bbn.marti.takcl.ignoreCoreConfig=true",
            f"-Dcom.bbn.marti.takcl.igniteIpAddressOverride={self.host}",
            "-Dcom.bbn.marti.takcl.igniteNetworkTimeout=10000",
            "-Dcom.bbn.marti.takcl.igniteClientFailureDetectionTimeout=30000",
            f"-Djava.io.tmpdir={directory / 'temporary'}",
            f"-Dio.netty.tmpdir={directory / 'temporary'}",
            f"-Dio.netty.native.workdir={directory / 'temporary'}",
            *(f"--add-opens={package}=ALL-UNNAMED" for package in _JAVA_OPENS),
        )


class TakJvmRuntime:
    """Start once under leadership; a process must exit after closing Java."""

    _owner: ClassVar["TakJvmRuntime | None"] = None
    _lock: ClassVar[Any] = RLock()

    def __init__(self, settings: TakJvmSettings | None = None) -> None:
        self.settings = settings or TakJvmSettings.from_env()
        self._pid = os.getpid()
        self._store: JniFileAuthStore | None = None
        self._helper: Any = None
        self._profile: Any = None
        self._closed = False

    def start(self) -> JniFileAuthStore:
        """Create the sole client lazily, after Kubernetes leadership is held."""
        with self._lock:
            if self._pid != os.getpid() or self._closed:
                raise RuntimeError("TAK runtime cannot start after closing or forking")
            if self._store is not None:
                return self._store
            if TakJvmRuntime._owner is not None and TakJvmRuntime._owner is not self:
                raise RuntimeError("Only one TAK JVM runtime is allowed per process")
            if not self.settings.jar.is_file():
                raise FileNotFoundError(self.settings.jar)
            configuration = self.settings.write_configuration()
            config = importlib.import_module("jnius_config")
            if TakJvmRuntime._owner is None:
                if config.vm_running:
                    raise RuntimeError("Configure TAK before importing jnius or starting a JVM")
                config.set_classpath(str(self.settings.jar.resolve()))
                config.add_options(*self.settings.options(configuration))
                # Ignite 2.18 reads this through System.getenv, not a -D option.
                os.environ["IGNITE_WORK_DIR"] = str(self.settings.directory.resolve() / "temporary")
                TakJvmRuntime._owner = self
            java = importlib.import_module("jnius")
            load = java.autoclass
            self._helper = load("com.bbn.marti.takcl.TakclIgniteHelper")
            profiles = load("com.bbn.marti.test.shared.data.servers.CLIImmutableServerProfiles")
            builder = load("com.bbn.marti.test.shared.data.servers.MutableServerProfile$Builder")
            self._profile = (
                builder.build(profiles.SERVER_CLI.getServer()).setHost(self.settings.host).setIdentifier("").create()
            )
            self._store = JniFileAuthStore(
                self._helper.getUserManager(self._profile),
                user_class=load("com.bbn.marti.xml.bindings.UserAuthenticationFile$User"),
                role_class=load("com.bbn.marti.xml.bindings.Role"),
                boolean_class=load("java.lang.Boolean"),
                ssl_helper=load("com.bbn.marti.takcl.SSLHelper"),
            )
            return self._store

    def close(self) -> None:
        """Close only this client's Ignite instance; the JVM lives until exit."""
        with self._lock:
            if self._pid != os.getpid():
                raise RuntimeError("TAK JVM cannot be used after forking")
            if self._closed:
                return
            self._closed = True
            if self._helper is not None and self._profile is not None:
                self._helper.closeAssociatedIgniteInstance(self._profile)
