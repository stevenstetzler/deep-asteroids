import click

from deep_asteroids.cli import main


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address -- never 0.0.0.0 on a shared host (salad's own serve.py convention)")
@click.option("--port", type=int, default=5000, show_default=True)
@click.option("--debug", is_flag=True, help="Flask's dev-server auto-reload + interactive debugger")
@click.pass_context
def serve(ctx, host, port, debug):
    """Run the live, per-request alternative to `package index`'s static site (#28's sketch
    -- see deep_asteroids/web/server.py's own module docstring for why).

    Flask's built-in dev server -- fine for this sketch/benchmark, not meant for real
    deployment (salad's own serve.py runs behind gunicorn for that)."""
    from deep_asteroids.database import get_db
    from deep_asteroids.web.server import create_app

    app = create_app(get_db())
    app.run(host=host, port=port, debug=debug)
