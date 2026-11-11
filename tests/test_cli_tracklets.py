import pytest
from click.testing import CliRunner

from deep_asteroids.cli import main


@pytest.fixture
def runner(db_url, deep_db_url):
    return CliRunner()


def _query(runner, *args):
    result = runner.invoke(main, ["query", "tracklets", *args])
    assert result.exit_code == 0, result.output
    return [line.split("|") for line in result.output.splitlines()]


def test_ingest_tracklets_all_sources(runner):
    result = runner.invoke(main, ["ingest", "tracklets"])
    assert result.exit_code == 0, result.output
    for source in ("synthetic", "mpc_observation", "mpchecker"):
        assert f"{source}: 1 night(s) ingested" in result.output
    rows = _query(runner, "source")
    assert len(rows) == 5 + 4 + 1

    again = runner.invoke(main, ["ingest", "tracklets"])
    assert "0 night(s) ingested, 1 skipped" in again.output
    forced = runner.invoke(main, ["ingest", "tracklets", "synthetic", "--force"])
    assert "synthetic: 1 night(s) ingested" in forced.output and "mpchecker" not in forced.output


def test_ingest_tracklets_one_source_and_night(runner):
    result = runner.invoke(main, ["ingest", "tracklets", "mpc-ephemeris", "--night", "20190402"])
    assert result.exit_code == 0, result.output
    assert _query(runner, "source", "provid", "n_on_chip") == [["mpchecker", "2019 GA179", "2"]]


def test_ingest_tracklets_rejects_unknown_source(runner):
    result = runner.invoke(main, ["ingest", "tracklets", "nope"])
    assert result.exit_code != 0


def test_query_tracklets_filters(runner):
    runner.invoke(main, ["ingest", "tracklets"])
    assert {r[0] for r in _query(runner, "component", "--orbit-id", "1002")} == {
        "primary", "companion", "barycenter", "photocenter"}
    assert _query(runner, "trkid", "--flag", "conflated_trkid") == [["00000FFq5p"]]
    flagged = {r[0] for r in _query(runner, "trkid", "--source", "mpc_observation",
                                    "--flag", "conflated_trkid", "--flag", "too_few_points")}
    assert flagged == {"00000FFq5p", "00000SING1"}
    assert _query(runner, "trkid", "--source", "mpc_observation", "--flag", "none") == [["00000E2QG2"]]
    assert _query(runner, "source", "--provid", "2019 GA179") == [["mpc_observation"], ["mpchecker"]]
    header, *rows = _query(runner, "night", "vra", "--header", "--source", "mpchecker")
    assert header == ["night", "vra"] and rows[0][0] == "20190402"


def test_query_tracklets_unknown_field(runner):
    result = runner.invoke(main, ["query", "tracklets", "nope"])
    assert result.exit_code != 0 and "unknown field" in result.output


def test_query_tracklets_output_file(runner, tmp_path):
    runner.invoke(main, ["ingest", "tracklets", "mpc-ephemeris"])
    out = tmp_path / "t.psv"
    result = runner.invoke(main, ["query", "tracklets", "provid", "--output", str(out)])
    assert result.exit_code == 0 and result.output == ""
    assert out.read_text() == "2019 GA179\n"
