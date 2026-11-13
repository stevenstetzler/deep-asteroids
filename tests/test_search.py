"""
Tests for ``util/detect.py`` (against a stand-in executable, not the real
``detect_and_measure``, which needs an opt_lsst build), ``search/detect.py``,
``search/catalog.py`` and ``search/run.py`` (a planted mover through the
real find-asteroids search, with a stub detector).
"""
import stat
import sys
from datetime import datetime

import numpy as np
import pytest
from astropy.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.database import create_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure
from deep_asteroids.database.search_run import SearchRun
from deep_asteroids.search.catalog import build_catalog
from deep_asteroids.search.detect import catalog_path, detect_exposures, remove_catalogs
from deep_asteroids.search.run import resolve_search_definitions, run_search_for_definition
from deep_asteroids.util.detect import LSSTDetector, parse_metadata

NIGHT = 20190402
RUN_ID = "deep-decam-T0-ccd1-20190402"
T0 = 58576.2
CADENCE = 0.0104  # ~15 minutes
N_EXPOSURES = 12
MOVER = {"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.1}  # deg, deg/day (dRA/dt, dDec/dt)

FAKE_BINARY = """#!/usr/bin/env python3
import sys
uri, args = sys.argv[1], sys.argv[2:]
opts = {}
i = 0
while i < len(args):
    if args[i] == "--quiet":
        i += 1
        continue
    opts[args[i]] = args[i + 1]
    i += 2
if uri.endswith("fail.fits"):
    print("boom", file=sys.stderr)
    sys.exit(3)
header = ("polarity,footprint_id,footprint_area,ix,iy,fx,fy,value,significance,"
          "ra_deg,dec_deg,flux,flux_sigma,snr,log_likelihood,mag")
rows = [] if uri.endswith("empty.fits") else [
    f"{opts['--threshold-polarity']},0,10,1,2,1.0,2.0,3.0,{opts['--sigma']},215.5,-13.5,100.0,10.0,10.0,-5.0,nan",
    f"{opts['--threshold-polarity']},1,12,3,4,3.0,4.0,3.5,6.0,215.6,-13.6,200.0,10.0,20.0,-9.0,22.5",
]
open(opts["--csv"], "w").write("\\n".join([header, *rows]) + "\\n")
open(opts["--metadata"], "w").write("MJD=58576.215349169448\\nVISIT=845580\\nDETECTOR=N11\\nIMAGE_PATH=" + uri + "\\n")
"""


@pytest.fixture
def engine_dir(tmp_path):
    d = tmp_path / "engine"
    d.mkdir()
    binary = d / "detect_and_measure"
    binary.write_text(FAKE_BINARY.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return d


def test_parse_metadata():
    meta = parse_metadata("MJD=58576.215349169448\nVISIT=845580\nIMAGE_PATH=/a/b.fits\n\nbad line")
    assert meta == {"MJD": 58576.215349169448, "VISIT": 845580, "IMAGE_PATH": "/a/b.fits"}


def test_detector_parses_csv_and_metadata(engine_dir):
    table = LSSTDetector(engine_dir=engine_dir, threshold=4.0, polarity="both").detect("/x/image.fits")
    assert table.colnames[:2] == ["polarity", "footprint_id"] and "ra" in table.colnames and "dec" in table.colnames
    assert list(table["ra"]) == [215.5, 215.6]
    assert list(table["polarity"]) == ["both", "both"] and list(table["significance"]) == [4.0, 6.0]
    assert np.isnan(table["mag"][0]) and table["mag"][1] == 22.5
    assert table.meta["MJD"] == 58576.215349169448 and table.meta["VISIT"] == 845580

    assert len(LSSTDetector(engine_dir=engine_dir).detect("/x/empty.fits")) == 0
    with pytest.raises(RuntimeError, match="boom"):
        LSSTDetector(engine_dir=engine_dir).detect("/x/fail.fits")


def test_detector_needs_the_binary(tmp_path):
    with pytest.raises(FileNotFoundError, match="build it first"):
        LSSTDetector(engine_dir=tmp_path)


class StubDetector:
    """Returns the planted mover (plus noise) for the exposure a uri names."""
    threshold, polarity = 5.0, "positive"

    def __init__(self, exposure_times, n_noise=20, empty=()):
        self.times, self.n_noise, self.empty, self.calls = exposure_times, n_noise, set(empty), []

    def detect(self, uri):
        self.calls.append(uri)
        i = int(uri.rsplit("_", 1)[1])
        if i in self.empty:
            ra = dec = np.array([], float)
        else:
            rng = np.random.default_rng(1000 + i)
            # the search sees TAI-converted times, but only time differences matter
            dt = self.times[i] - self.times[0]
            # the mover scatters ~0.2" about its line: an exactly collinear cluster makes the
            # refinement's covariance degenerate (as no real detection is)
            jitter = rng.normal(0, 5e-5, 2)
            ra = np.r_[MOVER["ra"] + MOVER["vra"] * dt + jitter[0], 215.0 + rng.uniform(-0.05, 0.05, self.n_noise)]
            dec = np.r_[MOVER["dec"] + MOVER["vdec"] * dt + jitter[1], -13.0 + rng.uniform(-0.05, 0.05, self.n_noise)]
        t = Table({"ra": ra, "dec": dec, "flux": np.full(len(ra), 100.0), "mag": np.full(len(ra), 22.0)})
        return t


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def definition(session):
    times = [T0 + i * CADENCE for i in range(N_EXPOSURES)]
    sd = SearchDefinition(
        night=NIGHT, field="T0", detector_number=1, detector_name="S1", run_id=RUN_ID, band=None,
        n_exposures=N_EXPOSURES, t_start_mjd=times[0], t_end_mjd=times[-1], created_at=datetime(2026, 1, 1),
    )
    for i, t in enumerate(times):
        sd.exposures.append(SearchDefinitionExposure(
            deep_db_exposure_id=100 + i, deep_db_detector_exposure_id=1000 + i, expnum=5000 + i, midpoint_mjd=t))
        session.add(Image(
            night=NIGHT, field="T0", detector_number=1, expnum=5000 + i, deep_db_exposure_id=100 + i,
            deep_db_detector_exposure_id=1000 + i, product_type="differenceExp", uri=f"/images/exp_{i}",
            created_at=datetime(2026, 1, 1)))
    session.add(sd)
    session.commit()
    return sd


@pytest.fixture
def detector(definition):
    return StubDetector([e.midpoint_mjd for e in definition.exposures])


def test_detect_exposures_is_idempotent(session, definition, detector, tmp_path):
    first = detect_exposures(session, definition, tmp_path / "cat", detector, workers=2)
    assert (first.n_written, first.n_skipped, first.n_detections) == (N_EXPOSURES, 0, N_EXPOSURES * 21)
    assert all(p.exists() for p in first.paths) and not list((tmp_path / "cat").glob("*.tmp"))
    t = Table.read(first.paths[0], format="ascii.ecsv")
    assert t.meta["deep_db_detector_exposure_id"] == 1000 and t.meta["image_uri"] == "/images/exp_0"
    assert t.meta["threshold"] == 5.0 and t.meta["polarity"] == "positive"

    again = detect_exposures(session, definition, tmp_path / "cat", detector)
    assert (again.n_written, again.n_skipped) == (0, N_EXPOSURES)
    assert len(detector.calls) == N_EXPOSURES
    forced = detect_exposures(session, definition, tmp_path / "cat", detector, force=True)
    assert forced.n_written == N_EXPOSURES


def test_detect_exposures_needs_images(session, definition, detector, tmp_path):
    session.delete(session.scalars(select(Image).where(Image.expnum == 5003)).one())
    session.commit()
    with pytest.raises(ValueError, match="no differenceExp image.*1003.*ingest images"):
        detect_exposures(session, definition, tmp_path, detector)


def test_remove_catalogs_is_exact(tmp_path):
    (tmp_path / "1.ecsv").write_text("x")
    (tmp_path / "2.ecsv").write_text("x")
    (tmp_path / "keep.txt").write_text("x")
    assert remove_catalogs(tmp_path, [tmp_path / "1.ecsv", tmp_path / "3.ecsv"]) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2.ecsv", "keep.txt"]
    with pytest.raises(ValueError, match="refusing"):
        remove_catalogs(tmp_path, [tmp_path / "keep.txt"])
    other = tmp_path / "sub"
    other.mkdir()
    (other / "9.ecsv").write_text("x")
    with pytest.raises(ValueError, match="refusing"):
        remove_catalogs(tmp_path, [other / "9.ecsv"])
    assert (other / "9.ecsv").exists()


def test_build_catalog(session, definition, tmp_path):
    detector = StubDetector([e.midpoint_mjd for e in definition.exposures], n_noise=4, empty=(2,))
    detect_exposures(session, definition, tmp_path, detector)
    cat = build_catalog(definition, tmp_path)
    assert cat.n_per_exposure[2] == 0 and sum(cat.n_per_exposure) == len(cat.table) == (N_EXPOSURES - 1) * 5
    t = cat.table
    assert t["ra"].unit == "deg" and t["time"].scale == "utc"
    # in time order, each labelled with exactly deep-db's exposure midpoint
    assert list(t["time"].mjd) == sorted(t["time"].mjd)
    assert t["time"].mjd[0] == T0
    assert set(t["time"].mjd) == {e.midpoint_mjd for e in definition.exposures if e.deep_db_detector_exposure_id != 1002}
    assert "flux" in t.colnames and "mag" in t.colnames


def test_build_catalog_errors_and_nan_columns(session, definition, tmp_path):
    with pytest.raises(FileNotFoundError, match="run detection first"):
        build_catalog(definition, tmp_path)
    for e in definition.exposures:  # a catalog with no flux/mag values at all
        Table({"ra": [215.0], "dec": [-13.0], "flux": [np.nan], "mag": [np.nan]}).write(
            catalog_path(tmp_path, e.deep_db_detector_exposure_id), format="ascii.ecsv")
    cat = build_catalog(definition, tmp_path)
    assert "flux" not in cat.table.colnames and "mag" not in cat.table.colnames
    for e in definition.exposures:
        Table({"ra": [], "dec": [], "flux": [], "mag": []}).write(
            catalog_path(tmp_path, e.deep_db_detector_exposure_id), format="ascii.ecsv", overwrite=True)
    with pytest.raises(ValueError, match="no detections"):
        build_catalog(definition, tmp_path)


def test_resolve_search_definitions(session, definition):
    assert resolve_search_definitions(session, night=NIGHT) == [definition]
    assert resolve_search_definitions(session, run_ids=[RUN_ID], band=None) == [definition]
    assert resolve_search_definitions(session, night=NIGHT, band="VR") == []
    assert resolve_search_definitions(session, night=NIGHT, detector_number=2) == []


SEARCH = {"velocity": (0.1, 0.5), "num_results": 3, "dx": 10.0}


def test_run_search_recovers_a_planted_mover(session, definition, detector, tmp_path):
    from find_asteroids.models import Gathered, Result, Search, Tracklet

    summary = run_search_for_definition(
        session, definition, tmp_path / "cat", detector, workers=2, **SEARCH)
    assert summary.ran and summary.n_exposures == N_EXPOSURES and summary.n_detections == N_EXPOSURES * 21
    # a noise cluster whose refinement fails is dropped by find-asteroids, so up to num_results come back
    assert 1 <= summary.n_results <= 3 and summary.n_catalogs_written == N_EXPOSURES
    assert f"{summary.n_results} result(s)" in summary.format()

    search = session.scalars(select(Search)).one()
    assert search.run_id == RUN_ID and (search.velocity_0, search.velocity_1, search.dx) == (0.1, 0.5, 10.0)
    assert search.results_dir is None and search.gpu is False

    best = session.scalars(select(Result).where(Result.result_num == 0)).one()
    assert best.n >= N_EXPOSURES - 1
    t = session.scalars(select(Tracklet).where(Tracklet.result_id == best.id)).one()
    assert t.vra == pytest.approx(MOVER["vra"], abs=0.01) and t.vdec == pytest.approx(MOVER["vdec"], abs=0.01)
    gathered = session.scalars(select(Gathered).where(Gathered.result_id == best.id)).all()
    assert len(gathered) >= N_EXPOSURES - 1 and all(g.mag == 22.0 for g in gathered)

    row = session.scalars(select(SearchRun)).one()
    assert (row.run_id, row.seed, row.threshold, row.polarity) == (RUN_ID, 0, 5.0, "positive")
    assert row.n_results == summary.n_results and row.search_seconds > 0 and row.find_asteroids_version and row.n_cpus >= 1


def test_run_search_is_idempotent_and_repeatable(session, definition, detector, tmp_path):
    from find_asteroids.models import Result, Tracklet

    def tracklets():
        return [(r.n, r.n1, r.n2, t.vra, t.vdec, t.ra_0, t.dec_0) for r, t in session.execute(
            select(Result, Tracklet).join(Tracklet, Tracklet.result_id == Result.id).order_by(Result.result_num))]

    run_search_for_definition(session, definition, tmp_path, detector, seed=7, **SEARCH)
    first = tracklets()
    skipped = run_search_for_definition(session, definition, tmp_path, detector, seed=7, **SEARCH)
    assert not skipped.ran and "skipped" in skipped.format() and tracklets() == first

    state = np.random.get_state()[1].copy()
    forced = run_search_for_definition(session, definition, tmp_path, detector, seed=7, force=True, **SEARCH)
    assert forced.ran and tracklets() == first  # same seed: identical, to the last bit
    assert (np.random.get_state()[1] == state).all()  # the global RNG state is restored
    assert len(session.scalars(select(SearchRun)).all()) == 1


def test_run_search_remove_catalogs(session, definition, detector, tmp_path):
    cat = tmp_path / "cat"
    cat.mkdir()
    (cat / "unrelated.txt").write_text("x")
    summary = run_search_for_definition(session, definition, cat, detector, remove_catalogs_after=True, **SEARCH)
    assert summary.n_catalogs_removed == N_EXPOSURES
    assert [p.name for p in cat.iterdir()] == ["unrelated.txt"]
    # a skipped run deletes nothing
    detect_exposures(session, definition, cat, detector)
    skipped = run_search_for_definition(session, definition, cat, detector, remove_catalogs_after=True, **SEARCH)
    assert not skipped.ran and len(list(cat.glob("*.ecsv"))) == N_EXPOSURES


def test_cli_search_run_and_detect(definition, monkeypatch, tmp_path):
    """The CLI end to end, with the detector class swapped for the stub (the real binary needs a build)."""
    from click.testing import CliRunner

    import deep_asteroids.util.detect as detect_module
    from deep_asteroids.cli import main

    stub_times = [e.midpoint_mjd for e in definition.exposures]
    monkeypatch.setattr(detect_module, "LSSTDetector", lambda **kw: StubDetector(stub_times))
    cat, runner = tmp_path / "cat", CliRunner()

    nothing = runner.invoke(main, ["search", "run"])
    assert nothing.exit_code != 0 and "select at least one search definition" in nothing.output
    no_match = runner.invoke(main, ["search", "run", "--night", "19000101"])
    assert no_match.exit_code != 0 and "no search definition matches" in no_match.output

    detect = runner.invoke(main, ["search", "detect", "--run-id", RUN_ID, "--catalog-dir", str(cat), "--workers", "2"])
    assert detect.exit_code == 0, detect.output
    assert f"{N_EXPOSURES} catalog(s) written" in detect.output and len(list(cat.glob("*.ecsv"))) == N_EXPOSURES

    run = runner.invoke(main, [
        "search", "run", "--night", str(NIGHT), "--band", "none", "--catalog-dir", str(cat), "--num-results", "3",
        "--seed", "3", "--remove-catalogs",
    ])
    assert run.exit_code == 0, run.output
    assert " result(s) from" in run.output and f"removed {N_EXPOSURES} catalog(s)" in run.output
    assert not list(cat.glob("*.ecsv"))
    again = runner.invoke(main, ["search", "run", "--run-id", RUN_ID, "--catalog-dir", str(cat)])
    assert "skipped" in again.output
