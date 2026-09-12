========================
OpenDefence TAK operator
========================

K8s operator that handles rasenmaeher-k8soperator CRDs to TAKServer


Docker and Podman
-----------------

For more controlled deployments and to get rid of "works on my computer" -syndrome, we always
make sure our software works under docker.

It's also a quick way to get started with a standard development environment.

Each command block offers Docker and Podman alternatives; run only the block
for your chosen engine. Both engines use the same Dockerfiles.

SSH agent forwarding
^^^^^^^^^^^^^^^^^^^^

Docker builds use buildkit_ (Podman does not need this setting)::

    export DOCKER_BUILDKIT=1

.. _buildkit: https://docs.docker.com/develop/develop-images/build_enhancements/

And also the exact way for forwarding agent to running instance is different on OSX::

    export DOCKER_SSHAGENT="-v /run/host-services/ssh-auth.sock:/run/host-services/ssh-auth.sock -e SSH_AUTH_SOCK=/run/host-services/ssh-auth.sock"

and Linux::

    export DOCKER_SSHAGENT="-v $SSH_AUTH_SOCK:$SSH_AUTH_SOCK -e SSH_AUTH_SOCK"

For Podman on Linux, use an agent socket accessible on the engine host::

    export PODMAN_SSHAGENT="-v $SSH_AUTH_SOCK:$SSH_AUTH_SOCK -e SSH_AUTH_SOCK"

For Podman Machine on macOS or Windows, omit runtime agent forwarding when using
the generated project's public dependencies::

    export PODMAN_SSHAGENT=""

The macOS launchd agent socket cannot be bind-mounted from inside the Linux VM.
If you add private SSH dependencies, configure an agent inside the VM and set
``PODMAN_SSHAGENT`` using its socket path, or run the commands on a Linux host
with an SSH agent. Build-time ``--ssh default`` is separate from runtime mounts.
Docker Desktop's ``/run/host-services/ssh-auth.sock`` path is specific to Docker Desktop.
See the `Podman build options <https://docs.podman.io/en/stable/markdown/podman-build.1.html>`_
for SSH forwarding options.

Creating a development container
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Build image, create container and start it::

    # Docker
    docker build --ssh default --target devel_shell -t takoperator:devel_shell .
    docker create --name takoperator_devel -v "$(pwd):/app" -it $(echo $DOCKER_SSHAGENT) takoperator:devel_shell
    docker start -i takoperator_devel

    # Podman alternative
    podman build --ssh default --target devel_shell -t takoperator:devel_shell .
    podman create --name takoperator_devel -v "$(pwd):/app" -it $(echo $PODMAN_SSHAGENT) takoperator:devel_shell
    podman start -i takoperator_devel

prek considerations
^^^^^^^^^^^^^^^^^^^^^^^^^

If working in Docker instead of native env you need to run the prek checks in docker too::

    # Docker
    docker exec -i takoperator_devel /bin/bash -c "uv run --locked prek install --install-hooks"
    docker exec -i takoperator_devel /bin/bash -c "uv run --locked prek run --all-files"

    # Podman alternative
    podman exec -i takoperator_devel /bin/bash -c "uv run --locked prek install --install-hooks"
    podman exec -i takoperator_devel /bin/bash -c "uv run --locked prek run --all-files"

You need to have the container running, see above. Or alternatively use the docker run syntax but using
the running container is faster::

    # Docker
    docker run --rm -it -v "$(pwd):/app" takoperator:devel_shell -c "uv run --locked prek run --all-files"

    # Podman alternative
    podman run --rm -it -v "$(pwd):/app" takoperator:devel_shell -c "uv run --locked prek run --all-files"

Test suite
^^^^^^^^^^

You can use the devel shell to run pytest when doing development. To test
multiple Python versions locally, use the "tox" target in the Dockerfile::

    # Docker
    docker build --ssh default --target tox -t takoperator:tox .
    docker run --rm -it -v "$(pwd):/app" $(echo $DOCKER_SSHAGENT) takoperator:tox

    # Podman alternative
    podman build --ssh default --target tox -t takoperator:tox .
    podman run --rm -it -v "$(pwd):/app" $(echo $PODMAN_SSHAGENT) takoperator:tox

Production docker
^^^^^^^^^^^^^^^^^

GitHub Actions builds the test and production targets from both Dockerfile_alpine
and Dockerfile_debian for pull requests. Publishing uses the default Dockerfile
(Alpine) for both linux/amd64 and linux/arm64. See the CI configuration below.

There's a "production" target as well for running the application. Tag the image
with the project version::

    # Docker
    docker build --ssh default --target production -t takoperator:0.1.0-260913 .
    docker run -it --name takoperator takoperator:0.1.0-260913

    # Podman alternative
    podman build --ssh default --target production -t takoperator:0.1.0-260913 .
    podman run -it --name takoperator takoperator:0.1.0-260913

Alpine considerations
^^^^^^^^^^^^^^^^^^^^^

Alpine images are much more lightweight than Debian/Ubuntu ones so they are preferred where possible.
There are a few potential issues however:

  - Compiled extensions not available as wheels are built from source in the builder stage.
  - Compiled extensions not compiling under Alpine. Alpine does not have certain nonstandard extensions to libc
    enabled by default, poorly written extensions will fail to compile because they depend on these extensions
    and do not explicitly request them to be enabled.
  - Commit uv.lock; Docker builds use uv sync --locked to detect stale dependency metadata.


Development
-----------

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
  and package builds on Python 3.12, 3.13 and 3.14, JUnit artifacts, and Alpine
  and Debian container builds.
- Pull requests from this repository: Snyk testing and image publishing after
  the version, metadata, prek, test and container build checks pass. Fork pull
  requests skip Snyk, publishing and the JUnit check report; their test artifacts
  are still uploaded. Snyk runs independently of the publishing gate.
- Pushes to main: Snyk monitoring and image publishing. Both workflows also
  support manual runs; the main workflow only runs its jobs on main, and manual
  runs of the pull request workflow skip version increment validation and
  publishing.

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
