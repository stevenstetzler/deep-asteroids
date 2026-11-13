import crossmatch_helpers as h
import pytest
from click.testing import CliRunner
from sqlalchemy.orm import Session

from deep_asteroids.cli import main
from deep_asteroids.crossmatch.classify import ResultRow, Thresholds, classify
from deep_asteroids.database import create_db
from deep_asteroids.viz.plots import plot_magnitudes, plot_residuals

PNG = b"\x89PNG\r\n\x1a\n"


def row(i, **kw):
    base = dict(result_id=i, run_id="r", result_num=i, night=h.NIGHT, authoritative=True, n_gathered=50,
                median_residual_arcsec=0.2, speed=0.2, sigma_v=1e-5, median_mag=22.0, velocity_0=0.1, velocity_1=0.5,
                best={})
    base.update(kw)
    return ResultRow(**base)


def rows():
    return classify([
        row(1, best={"mpc_linked": 0.1}, median_mag=20.0), row(2, best={"synthetic": 0.1}, median_mag=23.0),
        row(3, best={"mpchecker": 1.0}, median_mag=21.0), row(4, median_mag=23.5),  # putative new
        row(5, median_residual_arcsec=4.0, median_mag=22.5), row(6, median_residual_arcsec=4.0, speed=0.9),
        row(7, authoritative=False, median_mag=19.0),
    ])


def test_residual_and_magnitude_plots_are_written(tmp_path):
    thr = Thresholds()
    assert plot_residuals(rows(), tmp_path / "sub" / "r.png", thr).read_bytes()[:8] == PNG
    assert plot_residuals(rows(), tmp_path / "all.png", thr, authoritative_only=False).stat().st_size > 5000
    assert plot_magnitudes(rows(), tmp_path / "m.png", thr).read_bytes()[:8] == PNG
    assert plot_magnitudes(rows(), tmp_path / "m2.png", thr, authoritative_only=False, bin_width=1.0).exists()


def test_a_plot_of_nothing_says_so(tmp_path):
    with pytest.raises(ValueError, match="no results with a magnitude"):
        plot_magnitudes(classify([row(1, median_mag=None)]), tmp_path / "m.png")


def test_cli_plots(db_url, tmp_path):
    runner = CliRunner()
    empty = runner.invoke(main, ["plot", "residuals", "--out", str(tmp_path / "x.png")])  # a database with no tables yet
    assert empty.exit_code != 0 and "no results selected" in empty.output

    from deep_asteroids.crossmatch.statistics import compute_result_statistics

    with Session(create_db()) as session:
        h.add_definition(session, "run")
        a, b = h.add_run(session, "run", [{"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.05},
                                          {"ra": 215.05, "dec": -13.0, "vra": 0.25, "vdec": 0.0}])
        for _ in range(2):
            h.add_gathered(session, a, [0.2] * h.N_EXPOSURES, mags=[21.0] * h.N_EXPOSURES)
            h.add_gathered(session, b, [4.0] * h.N_EXPOSURES, mags=[23.0] * h.N_EXPOSURES, vra=0.25, vdec=0.0, ra=215.05)
        h.add_known(session, "mpc_linked", 215.0, -13.0, 0.2, 0.05, n_members=1)
        session.commit()
        unfinished = runner.invoke(main, ["plot", "residuals", "--out", str(tmp_path / "x.png")])
        assert unfinished.exit_code != 0 and "run `deep-asteroids statistics` first" in unfinished.output
        compute_result_statistics(session)
    assert runner.invoke(main, ["crossmatch"]).exit_code == 0

    for command in ("residuals", "magnitudes"):
        out = runner.invoke(main, ["plot", command, "--out", str(tmp_path / f"{command}.png"), "--night", str(h.NIGHT)])
        assert out.exit_code == 0, out.output
        assert (tmp_path / f"{command}.png").read_bytes()[:8] == PNG
    out = runner.invoke(main, ["plot", "residuals", "--out", str(tmp_path / "all.png"), "--all-results",
                               "--log10-residual", "0.5", "--min-gathered", "5"])
    assert out.exit_code == 0 and (tmp_path / "all.png").exists()
    assert "no results selected" in runner.invoke(
        main, ["plot", "magnitudes", "--out", str(tmp_path / "n.png"), "--night", "19000101"]).output
