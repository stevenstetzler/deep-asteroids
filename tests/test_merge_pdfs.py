"""Tests for `deep-asteroids package merge-pdfs` (#32/#33): merging already-rendered
per-result PDFs into one PDF per (night, group) -- mpchecker/synthetic/putative-new."""
import crossmatch_helpers as h
import pytest
from click.testing import CliRunner
from pypdf import PdfReader, PdfWriter
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.crossmatch.dedup import dedup_results
from deep_asteroids.crossmatch.match import crossmatch_results
from deep_asteroids.crossmatch.statistics import compute_result_statistics
from deep_asteroids.database import create_db
from deep_asteroids.web.site import result_dir

N = h.N_EXPOSURES
FIELD = "A0b"
DETECTOR = 1


@pytest.fixture
def session(db_url):
    with Session(create_db()) as session:
        yield session


@pytest.fixture
def runner():
    return CliRunner()


def _write_fake_pdf(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as f:
        writer.write(f)
    writer.close()


def _setup_night(session, web_dir, run_id="run", band=None):
    """One search with a known (mpchecker) match, a synthetic match, and an unmatched
    putative-new result -- each with its own rendered PDF already on disk, as `package
    result` would have left it. ``band`` is the search's band (``None``: the undefined-band legacy search)."""
    sd = h.add_definition(session, run_id, band=band)
    known_r, synth_r, new_r = h.add_run(session, run_id, [
        {"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05},   # known (mpchecker)
        {"ra": 215.05, "dec": -13.0, "vra": 0.25, "vdec": 0.0},  # synthetic
        {"ra": 215.0, "dec": -13.1, "vra": 0.3, "vdec": 0.02},   # unmatched, putative new
    ])
    for _ in range(2):
        h.add_gathered(session, known_r, [0.2] * N, mags=[21.0] * N)
        h.add_gathered(session, synth_r, [0.2] * N, vra=0.25, vdec=0.0, ra=215.05, mags=[20.0] * N)
        h.add_gathered(session, new_r, [0.2] * N, vra=0.3, vdec=0.02, dec=-13.1, mags=[22.0] * N)
    h.add_known(session, "mpchecker", 215.0, -13.0, 0.2, 0.05)
    h.add_known(session, "synthetic", 215.05, -13.0, 0.25, 0.0)
    session.commit()

    compute_result_statistics(session)
    crossmatch_results(session)
    dedup_results(session, band=band)

    for r, num in ((known_r, 0), (synth_r, 1), (new_r, 2)):
        _write_fake_pdf(result_dir(web_dir, sd, num) / f"{run_id}-result-{num}.pdf")
    return sd


def test_cli_merge_pdfs(session, tmp_path, runner):
    web_dir = tmp_path / "web"
    _setup_night(session, web_dir)

    result = runner.invoke(main, ["package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir), "--night", str(h.NIGHT)])
    assert result.exit_code == 0, result.output
    assert "3 merged PDF(s) written" in result.output

    mpchecker_pdf = web_dir / "pdfs" / str(h.NIGHT) / "mpchecker.pdf"
    synthetic_pdf = web_dir / "pdfs" / str(h.NIGHT) / "synthetic.pdf"
    putative_new_pdf = web_dir / "pdfs" / str(h.NIGHT) / "putative-new.pdf"
    assert len(PdfReader(str(mpchecker_pdf)).pages) == 1
    assert len(PdfReader(str(synthetic_pdf)).pages) == 1
    assert len(PdfReader(str(putative_new_pdf)).pages) == 1

    again = runner.invoke(main, ["package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir), "--night", str(h.NIGHT)])
    assert "3 already present" in again.output and "0 merged PDF(s) written" in again.output

    forced = runner.invoke(main, [
        "package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir), "--night", str(h.NIGHT), "--force",
    ])
    assert "3 merged PDF(s) written" in forced.output


def test_cli_merge_pdfs_narrows_by_group(session, tmp_path, runner):
    web_dir = tmp_path / "web"
    _setup_night(session, web_dir)

    result = runner.invoke(main, [
        "package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir), "--night", str(h.NIGHT), "--group", "synthetic",
    ])
    assert result.exit_code == 0, result.output
    assert (web_dir / "pdfs" / str(h.NIGHT) / "synthetic.pdf").exists()
    assert not (web_dir / "pdfs" / str(h.NIGHT) / "mpchecker.pdf").exists()
    assert not (web_dir / "pdfs" / str(h.NIGHT) / "putative-new.pdf").exists()


def test_cli_merge_pdfs_skips_empty_when_no_pdf_rendered(session, tmp_path, runner):
    web_dir = tmp_path / "web"
    sd = _setup_night(session, web_dir)
    # Remove the synthetic result's rendered PDF, as if `package result` hadn't gotten to it yet.
    (result_dir(web_dir, sd, 1) / "run-result-1.pdf").unlink()

    result = runner.invoke(main, [
        "package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir), "--night", str(h.NIGHT), "--group", "synthetic",
    ])
    assert result.exit_code == 0, result.output
    assert not (web_dir / "pdfs" / str(h.NIGHT) / "synthetic.pdf").exists()
    assert "0 merged PDF(s) written" in result.output
    assert "1 empty (no rendered result matched)" in result.output
    assert "1 result(s) skipped (no PDF rendered)" in result.output


def test_cli_merge_pdfs_authoritative_only_excludes_redetections(session, tmp_path, runner):
    web_dir = tmp_path / "web"
    _setup_night(session, web_dir)
    # A redetection of the known (mpchecker) result, in a second search -- de-duplicated away,
    # so should be excluded from the merge by default (--authoritative-only).
    sd2 = h.add_definition(session, "run2")
    (redetect,) = h.add_run(session, "run2", [{"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05}])
    for _ in range(2):
        h.add_gathered(session, redetect, [0.2] * N, mags=[21.0] * N)
    session.commit()
    compute_result_statistics(session, run_ids=["run2"])
    crossmatch_results(session, run_ids=["run2"])
    dedup_results(session)
    _write_fake_pdf(result_dir(web_dir, sd2, 0) / "run2-result-0.pdf")

    result = runner.invoke(main, [
        "package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir), "--night", str(h.NIGHT), "--group", "mpchecker",
    ])
    assert result.exit_code == 0, result.output
    assert len(PdfReader(str(web_dir / "pdfs" / str(h.NIGHT) / "mpchecker.pdf")).pages) == 1

    all_results = runner.invoke(main, [
        "package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir), "--night", str(h.NIGHT), "--group", "mpchecker",
        "--all-results", "--force",
    ])
    assert all_results.exit_code == 0, all_results.output
    assert len(PdfReader(str(web_dir / "pdfs" / str(h.NIGHT) / "mpchecker.pdf")).pages) == 2


def test_cli_merge_pdfs_merges_one_band_only(session, tmp_path, runner):
    """The same object found by a legacy (undefined-band) and a VR search: each band's merged PDFs hold its own pages."""
    web_dir = tmp_path / "web"
    _setup_night(session, web_dir)  # legacy
    _setup_night(session, web_dir, run_id="run-VR", band="VR")

    vr = runner.invoke(main, ["package", "merge-pdfs", "--web-dir", str(web_dir), "--night", str(h.NIGHT),
                              "--out-dir", str(tmp_path / "vr")])  # VR is the default band
    assert vr.exit_code == 0, vr.output
    assert "3 merged PDF(s) written" in vr.output and "run-VR-result-0.pdf" not in vr.output
    legacy = runner.invoke(main, ["package", "merge-pdfs", "--band", "none", "--web-dir", str(web_dir),
                                  "--night", str(h.NIGHT), "--out-dir", str(tmp_path / "legacy")])
    assert "3 merged PDF(s) written" in legacy.output
    for out in ("vr", "legacy"):
        for group in ("mpchecker", "synthetic", "putative-new"):
            assert len(PdfReader(str(tmp_path / out / str(h.NIGHT) / f"{group}.pdf")).pages) == 1  # not 2: bands apart
    assert runner.invoke(main, ["package", "merge-pdfs", "--band", "r", "--web-dir", str(web_dir),
                                "--out-dir", str(tmp_path / "r")]).exit_code == 0
    assert not (tmp_path / "r").exists()  # no search of that band: nothing written
