import click


@click.group()
@click.option("--log-level", default="INFO", help="Set the logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)")
@click.version_option(package_name="deep-asteroids")
@click.pass_context
def main(ctx, log_level):
    import logging

    logging.basicConfig(level=log_level.upper())
