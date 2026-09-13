"""CLI entrypoints for OpenDefence TAK operator"""

import logging

import click

from libadvian.logging import init_logging
from takoperator import __version__


LOGGER = logging.getLogger(__name__)


@click.group()
@click.version_option(version=__version__)
@click.option("-l", "--loglevel", help="Python log level, 10=DEBUG, 20=INFO, 30=WARNING, 40=CRITICAL", default=30)
@click.option("-v", "--verbose", count=True, help="Shorthand for info/debug loglevel (-v/-vv)")
def takoperator_cli(loglevel: int, verbose: int) -> None:
    """K8s operator that handles rasenmaeher-k8soperator CRDs to TAKServer"""
    if verbose == 1:
        loglevel = 20
    if verbose >= 2:
        loglevel = 10
    init_logging(loglevel)
    LOGGER.setLevel(loglevel)


@takoperator_cli.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "allow_interspersed_args": False}
)
@click.pass_context
def run(ctx: click.Context) -> None:
    """Reconcile platform Users against TAK Server."""
    from takoperator.app import create_application  # noqa: PLC0415

    create_application().main(["run", *ctx.args])


@takoperator_cli.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "allow_interspersed_args": False}
)
@click.pass_context
def manifests(ctx: click.Context) -> None:
    """Print operator RBAC and deployment manifests without starting the JVM."""
    from takoperator.app import create_application  # noqa: PLC0415

    create_application().main(["manifests", *ctx.args])
