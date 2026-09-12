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

You can use the devel shell to run py.test when doing development, for CI use
the "tox" target in the Dockerfile::

    # Docker
    docker build --ssh default --target tox -t takoperator:tox .
    docker run --rm -it -v "$(pwd):/app" $(echo $DOCKER_SSHAGENT) takoperator:tox

    # Podman alternative
    podman build --ssh default --target tox -t takoperator:tox .
    podman run --rm -it -v "$(pwd):/app" $(echo $PODMAN_SSHAGENT) takoperator:tox

Production docker
^^^^^^^^^^^^^^^^^

GitLab CI builds the production target from both Dockerfile_alpine and
Dockerfile_debian on every pipeline. The ``production-build`` jobs require a
runner configured for privileged Docker-in-Docker with ``/certs/client`` shared
between the job and service containers. See the
`GitLab Docker-in-Docker setup <https://docs.gitlab.com/ci/docker/docker_in_docker/>`_.

There's a "production" target as well for running the application. Tag the image
with the project version::

    # Docker
    docker build --ssh default --target production -t takoperator:0.1.0 .
    docker run -it --name takoperator takoperator:0.1.0

    # Podman alternative
    podman build --ssh default --target production -t takoperator:0.1.0 .
    podman run -it --name takoperator takoperator:0.1.0

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

Bump the project version with bump-my-version::

    uv run --locked bump-my-version bump patch
    uv lock

Use ``minor`` or ``major`` instead of ``patch`` as needed. The configuration in
.bumpversion.toml updates the package metadata, module version, version test,
and production image tags in this README. Commit these changes together with
uv.lock; version bumping does not automatically create a commit or Git tag.

The hook configuration remains in .pre-commit-config.yaml, which prek supports.
System hooks invoke tools through uv so they use the project environment.
