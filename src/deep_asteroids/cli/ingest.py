import click

from deep_asteroids.cli import main


@main.group()
@click.pass_context
def ingest(ctx):
    """Ingest search definitions and known-object tracklets from deep-db (phase 1)."""
