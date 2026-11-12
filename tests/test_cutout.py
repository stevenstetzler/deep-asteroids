"""
Tests for ``util/cutout.py``'s subprocess plumbing (CSV round-trip,
cutout-path bookkeeping, error handling) against a small stand-in
executable -- not the real vendored ``cutout_and_photometry`` binary,
which needs an opt_lsst build. The stand-in implements just enough of the
real CLI contract (``<uri> --positions PATH --csv PATH --quiet
[--cutout-dir DIR --cutout-size N]``, and the photometry CSV's columns)
to exercise ``LSSTCutoutPhotometry.measure()`` end to end.
"""
import stat
import sys

import pytest

from deep_asteroids.util.cutout import LSSTCutoutPhotometry, PhotometryRequest

FAKE_BINARY = """#!/usr/bin/env python3
import csv
import sys
from pathlib import Path

uri = sys.argv[1]
args = sys.argv[2:]
opts = {}
i = 0
while i < len(args):
    if args[i] == "--quiet":
        i += 1
        continue
    opts[args[i]] = args[i + 1]
    i += 2

with open(opts["--positions"], newline="") as f:
    positions = [(row[0], float(row[1]), float(row[2])) for row in csv.reader(f)]

cutout_dir = opts.get("--cutout-dir")
if cutout_dir:
    Path(cutout_dir).mkdir(parents=True, exist_ok=True)

with open(opts["--csv"], "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "in_bounds", "ra_deg", "dec_deg", "x", "y", "flux", "flux_sigma", "snr",
                "log_likelihood", "mag", "mag_err_lo", "mag_err_hi", "mask", "zero_point", "cutout_written"])
    for id_, ra, dec in positions:
        if ra < 0:  # sentinel: a negative ra means "out of bounds" for this fake
            w.writerow([id_, "0", str(ra), str(dec), "nan", "nan", "nan", "nan", "nan",
                        "nan", "nan", "nan", "nan", "0", "nan", "0"])
            continue
        cutout_written = "0"
        if cutout_dir:
            (Path(cutout_dir) / f"{id_}.fits").write_text("fake fits")
            cutout_written = "1"
        w.writerow([id_, "1", str(ra), str(dec), "100.5", "200.5", "42.0", "1.5", "28.0",
                    "-3.5", "20.1", "0.02", "0.03", "0", "31.4", cutout_written])
print(f"measured {len(positions)} position(s): image={uri}")
"""


@pytest.fixture
def engine_dir(tmp_path):
    engine_dir = tmp_path / "cutout_engine"
    engine_dir.mkdir()
    binary = engine_dir / "cutout_and_photometry"
    binary.write_text(f"#!{sys.executable}\n" + FAKE_BINARY.split("\n", 1)[1])
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    return engine_dir


def test_measure_reads_in_bounds_and_out_of_bounds(engine_dir):
    engine = LSSTCutoutPhotometry(engine_dir=engine_dir)
    requests = [PhotometryRequest(id="a", ra=215.0, dec=-13.0), PhotometryRequest(id="b", ra=-1.0, dec=-13.0)]
    records = {r.id: r for r in engine.measure("image.fits", requests)}

    assert records["a"].in_bounds is True
    assert records["a"].ra == 215.0 and records["a"].dec == -13.0
    assert records["a"].flux == 42.0
    assert records["a"].mag == 20.1
    assert records["a"].mask == 0
    assert records["a"].cutout_path is None  # no cutout_dir given

    assert records["b"].in_bounds is False
    assert records["b"].flux is None and records["b"].mag is None and records["b"].mask is None


def test_measure_writes_cutouts_only_for_in_bounds_positions(engine_dir, tmp_path):
    engine = LSSTCutoutPhotometry(engine_dir=engine_dir)
    requests = [PhotometryRequest(id="a", ra=215.0, dec=-13.0), PhotometryRequest(id="b", ra=-1.0, dec=-13.0)]
    cutout_dir = tmp_path / "cutouts"

    records = {r.id: r for r in engine.measure("image.fits", requests, cutout_dir=str(cutout_dir))}

    assert records["a"].cutout_path == str(cutout_dir / "a.fits")
    assert (cutout_dir / "a.fits").exists()
    assert records["b"].cutout_path is None
    assert not (cutout_dir / "b.fits").exists()


def test_measure_preserves_request_order(engine_dir):
    engine = LSSTCutoutPhotometry(engine_dir=engine_dir)
    requests = [PhotometryRequest(id=str(i), ra=215.0 + i, dec=-13.0) for i in range(5)]
    assert [r.id for r in engine.measure("image.fits", requests)] == [str(i) for i in range(5)]


def test_missing_binary_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError, match="cutout_and_photometry"):
        LSSTCutoutPhotometry(engine_dir=tmp_path / "nope")


def test_nonzero_exit_raises_runtime_error(tmp_path):
    engine_dir = tmp_path / "cutout_engine"
    engine_dir.mkdir()
    binary = engine_dir / "cutout_and_photometry"
    binary.write_text(f"#!{sys.executable}\nimport sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)

    engine = LSSTCutoutPhotometry(engine_dir=engine_dir)
    with pytest.raises(RuntimeError, match="boom"):
        list(engine.measure("image.fits", [PhotometryRequest(id="a", ra=215.0, dec=-13.0)]))
