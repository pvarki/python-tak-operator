========================
OpenDefence TAK operator
========================

K8s operator that handles rasenmaeher-k8soperator CRDs to TAKServer


Docker and Podman
-----------------

Docker and Podman provide reproducible builds and an optional development
environment. Use the host setup in Development_ for everyday work.

Each command block offers Docker and Podman alternatives; run only the block
for your chosen engine. Both engines use the same Temurin-based Dockerfile;
the default ``Dockerfile`` points to ``Dockerfile_temurin``.

Production uses ``eclipse-temurin:17-jre-resolute`` with Ubuntu Resolute's Python 3.14.
Build, development, test and tox stages use the matching Temurin JDK, with Python
headers and compilers available for native JNI extensions. ``JAVA_HOME`` points
to ``/opt/java/openjdk``; the production JRE includes ``lib/server/libjvm.so``.

The build copies ``/opt/tak/utils/UserManager.jar`` and ``/opt/tak/version.txt``
from ``ghcr.io/pvarki/tak-server:5.8-RELEASE-69`` into production and development
images. The bundled UserManager JAR provides the initial TAKServer classes for
JNI integration; the rest of the TAKServer image is not copied. The
``TAKSERVER_IMAGE`` build argument selects a different compatible artifact image.

Runtime wheels are built for Python 3.14 on Resolute and installed offline into
``/opt/venv``. Build mounts keep uv and the wheel archives out of the final image;
the JDK, Python headers and compilers stay in the build stages. This follows
`python-tak-rmapi PR 154 <https://github.com/pvarki/python-tak-rmapi/pull/154>`_.
The ``JAVA_RUNTIME_IMAGE`` build argument accepts a compatible Resolute Java runtime
for comparison builds; keep its Java major version aligned with ``TEMURIN_VERSION``
(17 by default) and its Python ABI aligned with the builder.

Optional development container
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Build image, create container and start it::

    # Docker
    docker build --target devel_shell -t takoperator:devel_shell .
    docker create --name takoperator_devel -v "$(pwd):/app" -it takoperator:devel_shell
    docker start -i takoperator_devel

    # Podman alternative
    podman build --target devel_shell -t takoperator:devel_shell .
    podman create --name takoperator_devel -v "$(pwd):/app" -it takoperator:devel_shell
    podman start -i takoperator_devel

Container checks
^^^^^^^^^^^^^^^^

Install Git hooks in the host environment as described in Development_. To also
run checks in the development image::

    # Docker
    docker run --rm -it -v "$(pwd):/app" takoperator:devel_shell -c "uv run --locked prek run --all-files"

    # Podman alternative
    podman run --rm -it -v "$(pwd):/app" takoperator:devel_shell -c "uv run --locked prek run --all-files"

Test suite
^^^^^^^^^^

Run pytest locally with ``uv run --locked pytest -v``. The optional ``tox``
container target provides an isolated Python test environment::

    # Docker
    docker build --target tox -t takoperator:tox .
    docker run --rm -it -v "$(pwd):/app" takoperator:tox

    # Podman alternative
    podman build --target tox -t takoperator:tox .
    podman run --rm -it -v "$(pwd):/app" takoperator:tox

Production docker
^^^^^^^^^^^^^^^^^

GitHub Actions builds the test and production targets from the default Temurin
Dockerfile for pull requests. Publishing uses the same Dockerfile for both
linux/amd64 and linux/arm64. See the CI configuration below.

CI runs the tests inside the test image, then checks the production CLI, Python
environment, TAKServer artifacts and JVM startup through JNI. The runtime check
also verifies that compilers, the JDK headers, pip, uv and wheel archives are absent.
To run it against a locally built image::

    docker run --rm -i takoperator:0.2.0-260913 python < docker/check-runtime.py
    # Podman alternative
    podman run --rm -i takoperator:0.2.0-260913 python < docker/check-runtime.py

There's a "production" target as well for running the application. Tag the image
with the project version::

    # Docker
    docker build --target production -t takoperator:0.2.0-260913 .
    docker run -it --name takoperator takoperator:0.2.0-260913

    # Podman alternative
    podman build --target production -t takoperator:0.2.0-260913 .
    podman run -it --name takoperator takoperator:0.2.0-260913

Commit uv.lock; Docker builds use uv sync --locked to detect stale dependency metadata.


Development
-----------

Python 3.14 or newer is required. Cloudcoil is sourced from Git tag ``0.8.0``
through ``tool.uv.sources`` in pyproject.toml; uv.lock records the resolved commit.
The tag's package metadata reports ``0.5.0dev0``, so the Git source determines
the version used here. See the
`Cloudcoil 0.8.0 guides <https://github.com/cloudcoil/cloudcoil/tree/0.8.0#choose-a-guide>`_.

TLDR:

- Install uv: https://docs.astral.sh/uv/getting-started/installation/
- Install project dependencies and prek hooks (also attempted during generation)::

    uv sync --locked
    uv run --locked prek install --install-hooks

- Run checks and tests::

    uv run --locked prek run --all-files
    uv run --locked pytest -v

Ruff handles linting and formatting; Pyrefly checks types. Run them individually with::

    uv run --locked ruff check src tests
    uv run --locked ruff format src tests
    uv run --locked pyrefly check

Use ``uv add PACKAGE`` for runtime dependencies and ``uv add --dev PACKAGE`` for
development tools. Commit both pyproject.toml and uv.lock after dependency changes.
Run ``uv lock`` after manually editing dependencies, and ``uv build`` to produce
wheel and source distributions.

Versions follow pvarki's Python convention ``MAJOR.MINOR.PATCH+YYMMDD``.
The ``release`` part records the release date and updates automatically when
bumping major, minor or patch. Container tags use ``-`` instead of ``+``;
the shared publisher normalizes this, and bump-my-version keeps the README's
local image tags in the same format.

Preview or bump the project version with bump-my-version::

    uv run --locked bump-my-version show-bump
    uv run --locked bump-my-version bump patch
    uv lock

Use ``minor`` or ``major`` instead of ``patch`` as needed, or ``release`` to
refresh only the date on a later day. The configuration in
.bumpversion.toml updates the package metadata, module version, version test,
and production image tags in this README. Commit these changes together with
uv.lock; version bumping does not automatically create a commit or Git tag.

The hook configuration remains in .pre-commit-config.yaml, which prek supports.
System hooks invoke tools through uv so they use the project environment.


CI configuration
----------------

GitHub Actions uses the shared actions in
`pvarki/config-ci-library <https://github.com/pvarki/config-ci-library>`_.
The workflows in .github/workflows cover:

- Pull requests: version increment and project metadata checks, prek, pytest
  and package builds on Python 3.14, JUnit artifacts, and Temurin
  container builds.
- Pull requests from this repository: Snyk testing and image publishing after
  the version, metadata, prek, test and container build checks pass. Fork pull
  requests skip Snyk, publishing and the JUnit check report; their test artifacts
  are still uploaded. Snyk runs independently of the publishing gate.
- Pushes to main: Snyk monitoring and image publishing. Both workflows also
  support manual runs; the main workflow only runs its jobs on main, and manual
  runs of the pull request workflow skip version increment validation and
  publishing.

Run the pull request workflow manually on a feature branch: all prek hooks,
including ``no-commit-to-branch``, remain enabled.

Published images use ``pvarki/tak-worker`` in GHCR, Docker Hub and ACR. The
publisher creates version and latest tags on main, and PR-specific tags for
pull requests. Configure these repository or organization settings:

- Variables: ``DOCKERHUB_USERNAME``, ``ACR_REPO`` (registry hostname),
  ``ACR_USERNAME``.
- Secrets: ``DOCKERHUB_TOKEN``, ``ACR_TOKEN``, ``SNYK_TOKEN``.
- The automatic ``GITHUB_TOKEN`` is granted ``packages: write`` by the
  publishing jobs. Snyk uses the ``deployapp-products`` organization.

All three registries are enabled by the workflow inputs. To disable Docker Hub
or ACR, remove all inputs for that registry from both publishing jobs. Passing
empty credentials makes the shared publisher fail.


Local TAK operator
------------------

The operator consumes the existing cluster-scoped ``User``, ``Group``, and
``Role`` resources in ``platform.opendefence.fi/v1alpha1``. Install their CRDs
and platform controller from ``../python-rasenmaeher-k8soperator`` first. This
repository does not install or change those CRDs.

The local Kustomize deployment uses the shared ``kind-rmk8soperator`` cluster
and isolates TAK, PostGIS, credentials, and storage in ``tak-operator-system``.
The TAK configuration, messaging, and API containers share a Pod and data volume,
following ``../docker-rasenmaeher-integration/takserver``. Credentials are generated
locally as Kubernetes Secrets; they are not committed. Existing Secrets are reused.
The deployment pins the TAK 5.8.69 image and digest validated by the JNI handoff.

Start TAK with ``task tak:up`` and inspect it with ``task tak:status``. If the
shared cluster is absent, run ``task cluster:up`` first. This delegates cluster
creation to the neighboring project. ``task tak:forward`` exposes HTTPS 8443 and
CoT TLS 8089 on the workstation. Removing a Deployment leaves its persistent
volumes intact; no cluster reset is required.

Build and start the operator with::

    task operator:build
    task operator:up
    task operator:logs

The build requires Docker Buildx; on the local macOS setup it uses Podman's Docker
API. The push task selects Podman or Docker using the neighboring project's runtime
convention. Images are pushed only to the shared local registry at ``localhost:5005``.

Run ``takoperator manifests`` to inspect operator RBAC without connecting to Java
or Kubernetes. Run ``takoperator run`` to start reconciliation. In a container,
configure ``CLOUDCOIL_NAMESPACE=tak-operator-system`` and use the deployment under
``deploy/operator``. The process joins Ignite only after acquiring its Kubernetes
Lease. All JNI operations use one dedicated thread; shutdown drains an in-flight
operation before closing the Ignite client. Do not fork after JVM startup.

Credential mapping
^^^^^^^^^^^^^^^^^^

The current platform CRD has no credential Secret reference. For now, annotate a
User with ``tak.opendefence.fi/credential-secret: <secret-name>``. The Secret must
be in the operator namespace and contain exactly one of these keys:

- ``password``: UTF-8 password for the TAK account.
- ``tls.crt``: PEM X.509 certificate whose TAK-derived subject matches
  ``spec.callsign``. Private keys are not required by the operator.

``spec.publicKey`` remains the platform's field. A public key alone cannot be
registered as a TAK X.509 certificate. The neighboring demo's placeholder keys
therefore require an explicit credential Secret for this implementation. Missing
credentials produce a waiting observation and create no TAK account. Secret
changes trigger reconciliation; password rotation uses Secret UID/resourceVersion
without storing password material or hashes in Kubernetes annotations.

For example, after applying the neighboring project's demo objects, supply Bob's
password from a local file and reference the Secret::

    kubectl --context kind-rmk8soperator apply -f ../python-rasenmaeher-k8soperator/examples/demo.yaml
    kubectl --context kind-rmk8soperator -n tak-operator-system create secret generic bob-tak \
      --from-file=password=/path/to/password
    kubectl --context kind-rmk8soperator annotate users.platform.opendefence.fi bob \
      tak.opendefence.fi/credential-secret=bob-tak

Only approved, non-revoked Users are provisioned. Revocation or removal of approval
deletes an account owned by that User. ``spec.callsign`` becomes immutable once
ownership is recorded. An already-existing unowned TAK account is a conflict;
there is no automatic adoption. Deletion removes the owned account and verifies
absence before releasing the finalizer. Set
``tak.opendefence.fi/deletion-policy: Retain`` to retain it on CR deletion.

Membership and observations
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Each User owns the ordinary TAK membership edges it adds through ``spec.groupRefs``;
the referenced Group's ``spec.name`` supplies the TAK group name. Removing a reference,
or deleting its Group, removes those owned edges. Existing manual ordinary memberships,
IN/OUT memberships, fingerprints, and server privileges survive membership changes.
The JNI bridge supports full directional snapshots and replacement, but the current
platform CRDs do not expose directional membership intent.

TAK has no independently persisted empty group. Group objects describe routing names;
their memberships are materialized by Users. Operational roles and platform role names
are never mapped to ``ROLE_ADMIN`` or other file-auth privileges. Durable operational
role assignment remains unsupported pending the separate discovery and live-client
validation described in ``references/tak_operational_role_greenfield_reconciliation.md``.

The platform controller retains ownership of ``status``. TAK publishes its result in
``tak.opendefence.fi/observed`` and a durable write-ahead ownership record in
``tak.opendefence.fi/ownership``. These contain no credential material. The operator
persists ownership before external writes, rereads TAK after mutations, and retries
from actual state after failures. A periodic pass repairs drift. Kubernetes resource
version checks and leader election reduce races, but Ignite writes are not distributed
transactions and cannot fence an external TAK administrator.

Ignite connectivity
^^^^^^^^^^^^^^^^^^^

TAK Server can bind Ignite to its Pod IP. The server's generated
``TAKIgniteConfig.xml`` sets ``igniteHost`` to the Downward API ``POD_IP``.
Discovery listens on TCP 47500; the three server JVMs use communication ports
47100-47102. A headless ``takserver`` Service exposes discovery for the operator
in a separate Pod.

The operator distinguishes the remote discovery seed (``TAK_IGNITE_HOST=takserver``)
from its own local address (``TAK_IGNITE_BIND_ADDRESS`` set from its Pod IP). Its
runtime generates its own TAKCL and Ignite XML; it needs no access to the server's
persistent volume. It sets the ``IGNITE_WORK_DIR`` environment variable before
starting Java; this Ignite version ignores the same-named JVM system property.
The deployment also uses its writable runtime volume as the working directory,
so the remaining filesystem can stay read-only. The TAKCL property
``com.bbn.marti.takcl.igniteIpAddressOverride`` selects the remote discovery seed.
Ignite communication is bidirectional between server and client Pod IPs; forwarding
only 47500 to localhost is insufficient. Keep this internal control plane reachable
only by trusted TAK and operator workloads. The included NetworkPolicy describes
the permitted local workload traffic; enforcement depends on the cluster CNI.

Validation
^^^^^^^^^^

Unit tests cover JNI conversion, ownership checkpoints, credential rotation,
partial-write recovery, finalizers, dependency resolution, and cancellation.
Local live checks used the shared kind cluster and the exact handoff JAR. They
verified password and certificate lifecycle, ordinary/IN/OUT replacement,
explicit server-role clearing, and idempotence from a separate JNI Pod. The
deployed operator reconciled the neighboring Bob and Charlie examples, including
group removal/restoration, password rotation, revocation/reactivation, and deletion
of an isolated finalizer fixture. Those results were checked against persisted
TAK state while preserving platform status. Durable operational roles require
the separate client-facing validation described in the reference plan.
