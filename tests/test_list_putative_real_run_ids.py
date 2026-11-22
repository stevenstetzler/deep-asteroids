"""results/list_putative_real_run_ids.py: which run_ids the PDF backfill renders, per band."""
import importlib.util
from pathlib import Path

import crossmatch_helpers as h
import pytest
from sqlalchemy.orm import Session

from deep_asteroids.crossmatch.dedup import dedup_results
from deep_asteroids.crossmatch.match import crossmatch_results
from deep_asteroids.crossmatch.statistics import compute_result_statistics
from deep_asteroids.database import create_db

SPEC = importlib.util.spec_from_file_location(
    "list_putative_real_run_ids", Path(__file__).parent.parent / "results" / "list_putative_real_run_ids.py")
lister = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lister)
N = h.N_EXPOSURES


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


def _search(session, run_id, band, good=True, detector=1):
    """One search with one result: tight (putative real) if ``good``, else noisy (sigma_v far over the cut)."""
    h.add_definition(session, run_id, band=band, detector=detector)
    (result,) = h.add_run(session, run_id, [{"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05,
                                              **({} if good else {"sigma_vra": 1e-3, "sigma_vdec": 1e-3})}])
    h.add_gathered(session, result, [0.2] * N, mags=[22.0] * N)


def test_run_ids_of_each_band(session):
    _search(session, "legacy", None)
    _search(session, "vr-good", "VR")
    _search(session, "vr-noise", "VR", good=False, detector=2)  # no putative-real result: not listed
    session.commit()
    compute_result_statistics(session)
    crossmatch_results(session)
    dedup_results(session)

    assert lister.putative_real_run_ids(session) == ["vr-good"]  # VR is the default band
    assert lister.putative_real_run_ids(session, band="VR") == ["vr-good"]
    assert lister.putative_real_run_ids(session, band=None) == ["legacy"]
    assert lister.putative_real_run_ids(session, band=Ellipsis) == ["legacy", "vr-good"]
    assert lister.putative_real_run_ids(session, band="r") == []
