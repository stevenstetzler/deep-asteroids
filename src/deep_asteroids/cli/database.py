import click
from deep_asteroids.cli import main


@main.group()
@click.pass_context
def database(ctx):
    """Create and inspect the deep-asteroids database; fetch deep-db."""
