import pytest
from click.testing import CliRunner
from sqlalchemy import inspect

from deep_asteroids.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.parametrize("args", [[], ["database"], ["ingest"], ["query"]])
def test_help(runner, args):
    result = runner.invoke(main, [*args, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output


def test_main_lists_command_groups(runner):
    result = runner.invoke(main, ["--help"])
    for group in ("database", "ingest", "query"):
        assert group in result.output


def test_database_create_and_force(runner, db_url):
    from deep_asteroids.database import get_db

    result = runner.invoke(main, ["database", "create"])
    assert result.exit_code == 0, result.output
    assert db_url in result.output
    assert "search" in inspect(get_db()).get_table_names()

    result = runner.invoke(main, ["database", "create", "--force"])
    assert result.exit_code == 0, result.output


def test_database_info(runner, db_url, deep_db_url):
    runner.invoke(main, ["database", "create"])
    result = runner.invoke(main, ["database", "info"])
    assert result.exit_code == 0, result.output
    assert db_url in result.output
    assert deep_db_url in result.output
    lines = {line.split()[0]: line.split()[-1] for line in result.output.splitlines() if line.startswith("  ")}
    assert lines["night"] == "1"
    assert lines["exposure"] == "3"
    assert lines["synthetic_object_ephemeris"] == "9"
    assert lines["search"] == "0"


def test_database_info_exact_matches_default(runner, db_url, deep_db_url):
    runner.invoke(main, ["database", "create"])
    default = runner.invoke(main, ["database", "info"]).output
    exact = runner.invoke(main, ["database", "info", "--exact"]).output
    assert default == exact


def test_database_info_without_deep_db(runner, db_url, tmp_path, monkeypatch):
    monkeypatch.delenv("DEEP_DB", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "empty-cache"))
    runner.invoke(main, ["database", "create"])
    result = runner.invoke(main, ["database", "info"])
    assert result.exit_code == 0, result.output
    assert "fetch-deep-db" in result.output
