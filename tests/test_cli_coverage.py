from datetime import datetime

import crossmatch_helpers as h
import pytest
from click.testing import CliRunner
from find_asteroids.models import Search as FASearch

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search_run import SearchRun


@pytest.fixture
def session(db_url):
    from sqlalchemy.orm import Session

    with Session(create_db()) as session:
        yield session


@pytest.fixture
def runner():
    return CliRunner()


def _query(runner, *args):
    result = runner.invoke(main, ["query", "coverage", *args])
    assert result.exit_code == 0, result.output
    return [line.split("|") for line in result.output.splitlines()]


def _add_images(session, sd, n, product_type="differenceExp"):
    for exposure in sd.exposures[:n]:
        session.add(Image(
            night=sd.night, field=sd.field, detector_number=sd.detector_number, expnum=exposure.expnum,
            deep_db_exposure_id=exposure.deep_db_exposure_id,
            deep_db_detector_exposure_id=exposure.deep_db_detector_exposure_id,
            product_type=product_type, uri=f"/repo/{exposure.expnum}.fits", created_at=datetime(2026, 1, 1),
        ))
    session.commit()


def _add_search_run(session, run_id, n_results=3):
    session.add(FASearch(run_id=run_id, velocity_0=0.1, velocity_1=0.5, angle_0=0.0, angle_1=359.99, dx=10.0,
                          num_results=50))
    session.add(SearchRun(run_id=run_id, seed=0, threshold=5.0, polarity="positive", n_exposures=h.N_EXPOSURES,
                          n_detections=100, n_results=n_results, created_at=datetime(2026, 1, 1)))
    session.commit()


def test_coverage_joins_images_and_search_run(session, runner):
    searched = h.add_definition(session, "run-searched", detector=1, band="VR")
    unsearched = h.add_definition(session, "run-unsearched", detector=2, band="VR")
    session.commit()
    _add_images(session, searched, n=h.N_EXPOSURES)
    _add_images(session, unsearched, n=3)  # partial coverage, never searched
    _add_search_run(session, "run-searched", n_results=7)

    rows = {r[4]: r for r in _query(runner, "--band", "VR")}
    assert set(rows) == {"run-searched", "run-unsearched"}

    searched_row = rows["run-searched"]
    assert searched_row[5] == str(h.N_EXPOSURES)  # n_exposures
    assert searched_row[6] == str(h.N_EXPOSURES)  # n_images
    assert searched_row[7] == "1"  # searched
    assert searched_row[8:14] == ["0.1", "0.5", "0.0", "359.99", "10.0", "50"]
    assert searched_row[14] == "7"  # n_results

    unsearched_row = rows["run-unsearched"]
    assert unsearched_row[5] == str(h.N_EXPOSURES)
    assert unsearched_row[6] == "3"
    assert unsearched_row[7] == "0"
    assert unsearched_row[8:] == [""] * 7


def test_coverage_product_type_and_unsearched_only(session, runner):
    sd = h.add_definition(session, "run-fakes", detector=3)
    session.commit()
    _add_images(session, sd, n=h.N_EXPOSURES, product_type="fakes_calexp")

    assert _query(runner)[0][6] == "0"  # default product type differenceExp: none ingested
    fakes_rows = _query(runner, "--product-type", "fakes_calexp")
    assert fakes_rows[0][6] == str(h.N_EXPOSURES)

    _add_search_run(session, "run-fakes")
    assert _query(runner, "--unsearched-only") == []


def test_coverage_filters_and_header(session, runner):
    h.add_definition(session, "run-a", night=20190402, field="A0b", detector=1)
    h.add_definition(session, "run-b", night=20190403, field="A0b", detector=1)
    session.commit()

    assert len(_query(runner, "--night", "20190402")) == 1
    header, *_ = _query(runner, "--header")
    assert header == [
        "night", "field", "detector_number", "band", "run_id", "n_exposures", "n_images", "searched",
        "velocity_min", "velocity_max", "angle_min", "angle_max", "dx", "num_results_requested", "n_results",
    ]
