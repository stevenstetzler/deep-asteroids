import click
from deep_asteroids.cli import main


@main.group()
@click.pass_context
def query(ctx):
    """Query the deep-asteroids database (phase 1)."""
