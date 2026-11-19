"""Batched rendering: a search's results are measured together, so each image is read once per batch, not once
per result (viz.result.measure_results / visualize_results, web.site.package_results, `package result`)."""
import collections

import pytest
import result_viz_helpers as h
from click.testing import CliRunner
from find_asteroids.models import Result, Search
from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.database import create_db
from deep_asteroids.viz.result import measure_result, measure_results, resolve_result, touched_exposures
from deep_asteroids.web.site import package_result, package_results, result_dir


class CountingEngine(h.FakeCutoutEngine):
    """The fake engine, recording how many times each image is opened and with how many positions."""

    def __init__(self):
        self.calls = collections.Counter()
        self.positions = collections.Counter()

    def measure(self, uri, requests, cutout_dir=None, cutout_size=51):
        requests = list(requests)
        self.calls[uri] += 1
        self.positions[uri] += len(requests)
        yield from super().measure(uri, requests, cutout_dir=cutout_dir, cutout_size=cutout_size)


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def definition(session):
    return h.build_definition(session)


@pytest.fixture
def result_nums(session, definition, tmp_path):
    h.run_planted_search(session, definition, tmp_path)
    nums = session.scalars(select(Result.result_num).join(Search, Search.id == Result.search_id)
                           .where(Search.run_id == h.RUN_ID).order_by(Result.result_num)).all()
    assert len(nums) >= 2  # the planted search returns several results: a batch has something to share
    return nums


def _jobs(session, nums):
    jobs = []
    for n in nums:
        sd, tracklet, gathered = resolve_result(session, h.RUN_ID, n)
        jobs.append((sd, tracklet, touched_exposures(sd, tracklet), gathered))
    return jobs


def _measurements(summary):
    return [(m.deep_db_detector_exposure_id, m.mjd_tai, m.in_bounds, m.mag, m.seen) for m in summary.measurements]


def test_each_image_is_read_once_for_all_results(session, result_nums, tmp_path):
    jobs = _jobs(session, result_nums)
    engine = CountingEngine()
    summaries = measure_results(session, engine, jobs, cutout_dir=tmp_path / "cut")
    assert len(summaries) == len(result_nums)
    assert set(engine.calls.values()) == {1}  # one engine call per image, however many results touch it
    for uri, n in engine.positions.items():
        assert n == sum(any(f"exp_{e.deep_db_detector_exposure_id - 1000}.fits" in uri for e in exposures)
                        for _sd, _t, exposures, _g in jobs)  # one position per result that crosses that image

    one_at_a_time = CountingEngine()
    for job in jobs:
        measure_result(session, one_at_a_time, *job, cutout_dir=tmp_path / "cut1")
    assert sum(one_at_a_time.calls.values()) == len(result_nums) * sum(engine.calls.values()) or \
        sum(one_at_a_time.calls.values()) > sum(engine.calls.values())  # the old way reads more


def test_batched_measurements_equal_one_at_a_time(session, result_nums, tmp_path):
    jobs = _jobs(session, result_nums)
    batched = measure_results(session, h.FakeCutoutEngine(), jobs, cutout_dir=tmp_path / "a")
    single = [measure_result(session, h.FakeCutoutEngine(), *job, cutout_dir=tmp_path / "b") for job in jobs]
    for b, s in zip(batched, single):
        assert _measurements(b) == _measurements(s)
        assert (b.n_exposures_touched, b.n_images_found) == (s.n_exposures_touched, s.n_images_found)
    threaded = measure_results(session, h.FakeCutoutEngine(), jobs, cutout_dir=tmp_path / "c", workers=3)
    assert [_measurements(t) for t in threaded] == [_measurements(b) for b in batched]  # overlapped reads, same result


def test_package_results_renders_a_run_with_one_read_per_image(session, definition, result_nums, tmp_path):
    web = tmp_path / "web"
    engine = CountingEngine()
    out = package_results(session, engine, definition, result_nums + [99], web, movie=False)
    assert out[99] == (False, None)  # no such result
    for n in result_nums:
        skipped, path = out[n]
        assert not skipped and path == result_dir(web, definition, n) / "index.html" and path.exists()
        assert (path.parent / f"{h.RUN_ID}-result-{n}.pdf").exists()
    assert set(engine.calls.values()) == {1} and len(engine.calls) <= h.N_EXPOSURES

    again = CountingEngine()
    out = package_results(session, again, definition, result_nums, web, movie=False)
    assert all(skipped for skipped, _ in out.values()) and not again.calls  # cached: nothing read
    forced = CountingEngine()
    package_results(session, forced, definition, result_nums[:1], web, movie=False, force=True)
    assert not any(skipped for skipped, _ in package_results(
        session, CountingEngine(), definition, result_nums[:1], web, movie=False, force=True).values())


def test_package_results_in_batches(session, definition, result_nums, tmp_path):
    """``batch_size`` trades memory for reads: one result per batch reads an image once per result that crosses it
    (one position per read), a batch of all of them reads it once."""
    together = CountingEngine()
    package_results(session, together, definition, result_nums, tmp_path / "a", movie=False)
    apart = CountingEngine()
    package_results(session, apart, definition, result_nums, tmp_path / "b", movie=False, batch_size=1)
    assert set(apart.positions.values()) == set(together.positions.values()) or sum(apart.positions.values()) == sum(
        together.positions.values())  # the same positions are measured either way
    assert sum(apart.calls.values()) == sum(apart.positions.values())  # one position per read
    assert sum(together.calls.values()) < sum(apart.calls.values())  # sharing the reads is the point


def test_package_result_is_the_one_result_case(session, definition, result_nums, tmp_path):
    engine = CountingEngine()
    skipped, path = package_result(session, engine, definition, result_nums[0], tmp_path / "web", movie=False)
    assert not skipped and path.exists() and set(engine.calls.values()) == {1}


def test_cli_package_result_batches_a_run(session, definition, result_nums, tmp_path, monkeypatch):
    engines = []

    def engine_factory():
        engines.append(CountingEngine())
        return engines[-1]

    monkeypatch.setattr("deep_asteroids.util.cutout.LSSTCutoutPhotometry", engine_factory)
    out = CliRunner().invoke(main, ["package", "result", "--run-id", h.RUN_ID, "--no-movie", "--workers", "2",
                                    "--web-dir", str(tmp_path / "web")])
    assert out.exit_code == 0, out.output
    assert f"{len(result_nums)} page(s) rendered" in out.output
    (engine,) = engines  # one engine, one pass over the run's images for all its results
    assert set(engine.calls.values()) == {1}
