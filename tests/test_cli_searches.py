import pytest
from click.testing import CliRunner

from deep_asteroids.cli import main

NIGHT = 20190402
FIELD = "A0b"


@pytest.fixture
def runner(db_url, deep_db_url):
    return CliRunner()


def _query(runner, *args):
    result = runner.invoke(main, ["query", "searches", *args])
    assert result.exit_code == 0, result.output
    return [line.split("|") for line in result.output.splitlines()]


def test_ingest_searches(runner):
    result = runner.invoke(main, ["ingest", "searches"])
    assert result.exit_code == 0, result.output
    assert "1 night(s) ingested" in result.output

    again = runner.invoke(main, ["ingest", "searches"])
    assert "0 night(s) ingested, 1 skipped" in again.output
    forced = runner.invoke(main, ["ingest", "searches", "--force"])
    assert "1 night(s) ingested" in forced.output


def test_query_searches_band_filter(runner):
    """Every exposure here is band 'VR', so each detector has a 'VR' row
    and an undefined-band (band=NULL) row with the same exposure count."""
    runner.invoke(main, ["ingest", "searches"])

    vr_rows = _query(runner, "detector_number", "n_exposures", "--band", "VR")
    assert {r[0] for r in vr_rows} == {"4", "5"}
    assert all(r[1] == "3" for r in vr_rows)

    undefined_rows = _query(runner, "detector_number", "n_exposures", "band", "--band", "")
    assert {r[0] for r in undefined_rows} == {"4", "5"}
    assert all(r[1:] == ["3", ""] for r in undefined_rows)  # band prints as "" for NULL

    both = _query(runner, "band", "--band", "VR", "--band", "")
    assert sorted(r[0] for r in both) == ["", "", "VR", "VR"]

    assert _query(runner, "band", "--detector-number", "4", "--band", "nope") == []


def test_query_searches_filters(runner):
    runner.invoke(main, ["ingest", "searches"])
    rows = _query(runner, "run_id", "band", "--night", str(NIGHT), "--field", FIELD, "--detector-number", "4")
    assert {r[1] for r in rows} == {"VR", ""}
    for run_id, band in rows:
        base = f"deep-decam-{FIELD}-ccd4-{NIGHT}"
        assert run_id == (base if band == "" else f"{base}-{band}")

    header, *_ = _query(runner, "night", "field", "--header")
    assert header == ["night", "field"]


def test_query_searches_unknown_field(runner):
    result = runner.invoke(main, ["query", "searches", "nope"])
    assert result.exit_code != 0 and "unknown field" in result.output
