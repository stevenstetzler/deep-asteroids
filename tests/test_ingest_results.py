import astropy.table
import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.ingest.results import ingest_results_db, ingest_results_dir, run_id_night
from find_asteroids.models import Base, Gathered, Points, Result, Search, Tracklet

RUN_ID = "deep-decam-A0b-ccd4-20190402"
OTHER_RUN_ID = "deep-decam-A0b-ccd5-20190403"


def _write_results_dir(path, n_results=2):
    """A run_search()-shaped results dir: <path>/<result_num>/{result,gathered,points,tracklet}.ecsv."""
    for num in range(n_results):
        d = path / str(num)
        d.mkdir(parents=True)
        astropy.table.Table(
            {"x": [10 + num], "y": [20], "direction": [3], "n": [4], "n1": [1], "n2": [2], "n5": [3], "n10": [4],
             "score": [0.5]}
        ).write(d / "result.ecsv", format="ascii.ecsv")
        astropy.table.Table(
            {"ra": [1.0, 1.1, 1.2], "dec": [2.0, 2.1, 2.2], "time": [58000.0, 58000.1, 58000.2],
             "flux": [5.0, 6.0, 7.0], "mag": [22.0, 21.9, 21.8], "image_id": [1, 2, 3]}
        ).write(d / "gathered.ecsv", format="ascii.ecsv")
        astropy.table.Table({"ra": [1.0, 1.2], "dec": [2.0, 2.2], "time": [58000.0, 58000.2]}).write(
            d / "points.ecsv", format="ascii.ecsv"
        )
        astropy.table.Table(
            {"vra": [0.1], "vdec": [0.2], "ra_0": [3.0], "dec_0": [4.0], "tref": [58000.1]}
        ).write(d / "tracklet.ecsv", format="ascii.ecsv")


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def source_url(tmp_path):
    """A find-asteroids results database with two runs (3 + 1 results)."""
    url = f"sqlite:///{tmp_path / 'source.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for run_id, n in ((RUN_ID, 3), (OTHER_RUN_ID, 1)):
            search = Search(run_id=run_id, velocity_0=0.1, num_results=50)
            s.add(search)
            s.flush()
            for num in range(n):
                result = Result(search_id=search.id, result_num=num, x=num, y=1, n=5, extra={"k": num})
                s.add(result)
                s.flush()
                s.add_all([
                    Gathered(result_id=result.id, ra=1.0, dec=2.0, time=58000.0, flux=3.0, mag=22.0, extra={"image_id": 7}),
                    Gathered(result_id=result.id, ra=1.1, dec=2.1, time=58000.1, extra={}),
                    Points(result_id=result.id, ra=1.0, dec=2.0, time=58000.0, extra={}),
                    Tracklet(result_id=result.id, vra=0.1, vdec=0.2, ra_0=3.0, dec_0=4.0, tref=58000.0, extra={}),
                ])
        s.commit()
    engine.dispose()
    return url


def _counts(session):
    return {
        m.__name__: session.scalar(select(func.count()).select_from(m))
        for m in (Search, Result, Gathered, Points, Tracklet)
    }


def test_run_id_night():
    assert run_id_night(RUN_ID) == 20190402
    assert run_id_night(RUN_ID + "-VR") == 20190402
    assert run_id_night("deep-B1b-ccd20") is None


def test_ingest_results_dir(session, tmp_path):
    _write_results_dir(tmp_path / "results")
    summary = ingest_results_dir(session, tmp_path / "results", RUN_ID)
    assert summary.run_ids == [RUN_ID]
    assert (summary.n_results, summary.n_gathered, summary.n_points, summary.n_tracklets) == (2, 6, 4, 2)
    assert summary.unmatched_run_ids == [RUN_ID]  # no search_definition ingested here
    assert _counts(session) == {"Search": 1, "Result": 2, "Gathered": 6, "Points": 4, "Tracklet": 2}

    search = session.scalars(select(Search)).one()
    assert search.output_format == "ecsv" and search.results_dir == str((tmp_path / "results").resolve())
    results = session.scalars(select(Result).order_by(Result.result_num)).all()
    assert [r.x for r in results] == [10, 11]
    assert results[0].extra == {"score": 0.5}  # unknown columns survive in extra
    gathered = results[1].gathered_entries
    assert sorted(g.mag for g in gathered) == [21.8, 21.9, 22.0]
    assert gathered[0].extra == {"image_id": 1} or {g.extra["image_id"] for g in gathered} == {1, 2, 3}
    assert results[1].tracklet_entries[0].tref == 58000.1


def test_ingest_results_dir_idempotent_and_force(session, tmp_path):
    _write_results_dir(tmp_path / "results")
    ingest_results_dir(session, tmp_path / "results", RUN_ID)
    again = ingest_results_dir(session, tmp_path / "results", RUN_ID)
    assert again.skipped_run_ids == [RUN_ID] and not again.run_ids
    assert _counts(session)["Result"] == 2
    forced = ingest_results_dir(session, tmp_path / "results", RUN_ID, force=True)
    assert forced.run_ids == [RUN_ID]
    assert _counts(session) == {"Search": 1, "Result": 2, "Gathered": 6, "Points": 4, "Tracklet": 2}


def test_ingest_results_dir_missing(session, tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_results_dir(session, tmp_path / "nope", RUN_ID)


def test_ingest_results_db_copies_and_remaps_ids(session, source_url):
    source = create_engine(source_url)
    summary = ingest_results_db(session, source, nights=[20190402])
    assert summary.run_ids == [RUN_ID]
    assert (summary.n_results, summary.n_gathered, summary.n_points, summary.n_tracklets) == (3, 6, 3, 3)

    # a second run lands after it, with colliding source ids remapped
    summary = ingest_results_db(session, source, run_ids=[OTHER_RUN_ID])
    assert summary.n_results == 1
    assert _counts(session) == {"Search": 2, "Result": 4, "Gathered": 8, "Points": 4, "Tracklet": 4}
    for result in session.scalars(select(Result)):
        assert len(result.gathered_entries) == 2 and len(result.tracklet_entries) == 1
        assert result.search.run_id in (RUN_ID, OTHER_RUN_ID)
    first = session.scalars(select(Result).join(Search).where(Search.run_id == RUN_ID, Result.result_num == 2)).one()
    assert first.extra == {"k": 2}
    assert sorted(g.mag for g in first.gathered_entries if g.mag is not None) == [22.0]
    assert session.scalars(select(Search).where(Search.run_id == RUN_ID)).one().velocity_0 == 0.1
    source.dispose()


def test_ingest_results_db_skip_and_force(session, source_url):
    source = create_engine(source_url)
    ingest_results_db(session, source)
    again = ingest_results_db(session, source)
    assert sorted(again.skipped_run_ids) == [RUN_ID, OTHER_RUN_ID] and not again.run_ids
    forced = ingest_results_db(session, source, run_ids=[RUN_ID], force=True)
    assert forced.run_ids == [RUN_ID]
    assert _counts(session) == {"Search": 2, "Result": 4, "Gathered": 8, "Points": 4, "Tracklet": 4}
    source.dispose()


def test_matches_search_definition(session, deep_db_url, source_url):
    from deep_asteroids.database import get_deep_db
    from deep_asteroids.ingest.searches import ingest_searches

    ingest_searches(session, get_deep_db())
    source = create_engine(source_url)
    summary = ingest_results_db(session, source)
    assert summary.unmatched_run_ids == [OTHER_RUN_ID]  # ccd5 on 20190403: no such definition
    source.dispose()


def test_cli(db_url, source_url, tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, ["ingest", "results", "--results-db", source_url, "--night", "20190402"])
    assert result.exit_code == 0, result.output
    assert "1 run(s) ingested" in result.output and "3 result(s)" in result.output

    _write_results_dir(tmp_path / "results")
    result = runner.invoke(main, ["ingest", "results", "--results-dir", str(tmp_path / "results"), "--run-id", "x-20190402"])
    assert result.exit_code == 0, result.output
    assert "2 result(s)" in result.output

    bad = runner.invoke(main, ["ingest", "results"])
    assert bad.exit_code != 0 and "exactly one" in bad.output
    bad = runner.invoke(main, ["ingest", "results", "--results-dir", str(tmp_path)])
    assert bad.exit_code != 0 and "--run-id" in bad.output
