"""Shared builders for viz/result.py and web/site.py tests: a search
definition with images already ingested, a stub detector planting one
mover, and a fake cutout engine -- plain functions/classes (not pytest
fixtures), like crossmatch_helpers.py, so both test modules can build
their own fixtures from them without re-exporting fixture names."""
from datetime import datetime
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.table import Table

from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure
from deep_asteroids.util.cutout import CutoutPhotometryEngine, PhotometryRecord

NIGHT = 20190403
FIELD = "T1"
DETECTOR = 1
RUN_ID = "deep-decam-T1-ccd1-20190403"
T0 = 58577.2
CADENCE = 0.0104  # ~15 minutes
N_EXPOSURES = 12
MOVER = {"ra": 215.0, "dec": -13.0, "vra": 0.2, "vdec": 0.1}
SEARCH = {"velocity": (0.1, 0.5), "num_results": 3, "dx": 10.0}


class FakeCutoutEngine(CutoutPhotometryEngine):
    """Deterministic stand-in for LSSTCutoutPhotometry (same convention as
    test_viz_tracklet.py's): ra < 0 is the out-of-bounds sentinel;
    everything else gets a FITS cutout (if cutout_dir is given) and a mag
    that varies by request id, so a light curve has something to plot."""

    def measure(self, uri, requests, cutout_dir=None, cutout_size=51):
        for r in requests:
            in_bounds = r.ra >= 0
            index = int(r.id.split("_")[0])
            mag = 20.0 + (index % 5) * 0.1 if in_bounds else None
            cutout_path = None
            if in_bounds and cutout_dir is not None:
                Path(cutout_dir).mkdir(parents=True, exist_ok=True)
                data = np.full((cutout_size, cutout_size), float(index % 7))
                hdul = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(data=data, name="IMAGE")])
                cutout_path = str(Path(cutout_dir) / f"{r.id}.fits")
                hdul.writeto(cutout_path, overwrite=True)
            yield PhotometryRecord(id=r.id, in_bounds=in_bounds, ra=r.ra, dec=r.dec, mag=mag,
                                   mag_err_lo=0.01, mag_err_hi=0.02, cutout_path=cutout_path)


class StubDetector:
    """Returns the planted mover (plus noise) for the exposure a uri names (matches test_search.py)."""
    threshold, polarity = 5.0, "positive"

    def __init__(self, exposure_times, n_noise=20):
        self.times, self.n_noise = exposure_times, n_noise

    def detect(self, uri):
        i = int(uri.rsplit("_", 1)[1].removesuffix(".fits"))
        rng = np.random.default_rng(1000 + i)
        dt = self.times[i] - self.times[0]
        jitter = rng.normal(0, 5e-5, 2)
        ra = np.r_[MOVER["ra"] + MOVER["vra"] * dt + jitter[0], 215.0 + rng.uniform(-0.05, 0.05, self.n_noise)]
        dec = np.r_[MOVER["dec"] + MOVER["vdec"] * dt + jitter[1], -13.0 + rng.uniform(-0.05, 0.05, self.n_noise)]
        return Table({"ra": ra, "dec": dec, "flux": np.full(len(ra), 100.0), "mag": np.full(len(ra), 22.0)})


def build_definition(session):
    """A SearchDefinition with N_EXPOSURES exposures, each with a differenceExp Image already ingested."""
    times = [T0 + i * CADENCE for i in range(N_EXPOSURES)]
    sd = SearchDefinition(
        night=NIGHT, field=FIELD, detector_number=DETECTOR, detector_name=f"S{DETECTOR}", run_id=RUN_ID, band=None,
        n_exposures=N_EXPOSURES, t_start_mjd=times[0], t_end_mjd=times[-1], created_at=datetime(2026, 1, 1),
    )
    for i, t in enumerate(times):
        sd.exposures.append(SearchDefinitionExposure(
            deep_db_exposure_id=100 + i, deep_db_detector_exposure_id=1000 + i, expnum=5000 + i, midpoint_mjd=t))
        session.add(Image(
            night=NIGHT, field=FIELD, detector_number=DETECTOR, expnum=5000 + i, deep_db_exposure_id=100 + i,
            deep_db_detector_exposure_id=1000 + i, product_type="differenceExp", uri=f"file:///fake/exp_{i}.fits",
            created_at=datetime(2026, 1, 1)))
    session.add(sd)
    session.commit()
    return sd


def run_planted_search(session, sd, tmp_path):
    """Runs a real search (a planted mover against a stub detector) over ``sd`` so there's a
    genuine Result/Tracklet/Gathered row to visualize; returns the best result's result_num (0)."""
    from deep_asteroids.search.run import run_search_for_definition

    detector = StubDetector([e.midpoint_mjd for e in sd.exposures])
    summary = run_search_for_definition(session, sd, tmp_path / "cat", detector, workers=2, **SEARCH)
    assert summary.ran and summary.n_results >= 1
    return 0
