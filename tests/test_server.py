"""Tests for web/server.py (#28's live-query sketch): the same nights -> fields -> CCDs ->
bands -> results drill-down web/index.py's build_site_index() writes as static files,
served per-request instead. See test_package_index.py for the static-site fixture this
reuses, and web/server.py's own module docstring for the real-database timing this was
built to measure (the drill-down pages are fast enough to serve live; the two
whole-survey rollups are not, and should stay cached/periodic)."""
import crossmatch_helpers as h
import pytest
from sqlalchemy.orm import Session

from deep_asteroids.database import create_db, get_db
from deep_asteroids.web.server import create_app

NIGHT_1 = 20190402
NIGHT_2 = 20190403


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def seeded(session):
    """Same shape as test_package_index.py's own fixture: one searched search definition
    with 2 results (night 1, A0b, ccd1, VR), one defined but unsearched (night 1, A0b, ccd2,
    band None/"all"), one searched on a different night/field/band (night 2, A0c, ccd5, g)."""
    mover = {"ra": h.BASE[0], "dec": h.BASE[1], "vra": 0.2, "vdec": 0.05, "n": 12}
    h.add_definition(session, "run-searched", night=NIGHT_1, field="A0b", detector=1, band="VR")
    h.add_run(session, "run-searched", [mover, {**mover, "ra": h.BASE[0] + 1.0}])
    h.add_definition(session, "run-unsearched", night=NIGHT_1, field="A0b", detector=2, band=None)
    h.add_definition(session, "run-other-night", night=NIGHT_2, field="A0c", detector=5, band="g")
    h.add_run(session, "run-other-night", [mover])
    session.commit()


@pytest.fixture
def client(seeded):
    app = create_app(get_db())
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_index_lists_nights_and_links_to_search_coverage(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'href="/results/{NIGHT_1}/"' in body and f'href="/results/{NIGHT_2}/"' in body
    assert 'href="/search/"' in body


def test_search_coverage_matches_static_site_shape(client):
    resp = client.get("/search/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'href="/results/{NIGHT_1}/A0b/ccd1/"' in body
    assert ">VR<" in body and ">(all)<" in body


def test_drill_down_through_fields_ccds_bands_results(client):
    fields = client.get(f"/results/{NIGHT_1}/").get_data(as_text=True)
    assert 'href="/results/20190402/A0b/"' in fields

    ccds = client.get(f"/results/{NIGHT_1}/A0b/").get_data(as_text=True)
    assert 'href="/results/20190402/A0b/ccd1/"' in ccds and 'href="/results/20190402/A0b/ccd2/"' in ccds

    bands = client.get(f"/results/{NIGHT_1}/A0b/ccd1/").get_data(as_text=True)
    assert 'href="/results/20190402/A0b/ccd1/VR/"' in bands and ">yes<" in bands

    unsearched_bands = client.get(f"/results/{NIGHT_1}/A0b/ccd2/").get_data(as_text=True)
    assert 'href="/results/20190402/A0b/ccd2/all/"' in unsearched_bands and ">no<" in unsearched_bands

    results = client.get(f"/results/{NIGHT_1}/A0b/ccd1/VR/").get_data(as_text=True)
    assert 'href="/results/20190402/A0b/ccd1/VR/0/"' in results and 'href="/results/20190402/A0b/ccd1/VR/1/"' in results


def test_unknown_path_segments_404(client):
    assert client.get("/results/19000101/").status_code == 404
    assert client.get(f"/results/{NIGHT_1}/NoSuchField/").status_code == 404
    assert client.get(f"/results/{NIGHT_1}/A0b/ccd99/").status_code == 404
    assert client.get(f"/results/{NIGHT_1}/A0b/ccd1/not-a-band/").status_code == 404
