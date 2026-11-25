"""
Source detection on one image, via the vendored ``detect_and_measure`` C++
engine (``detect_engine/`` -- see that directory's ``VENDORED_FROM.md`` for
provenance and how to build it). No LSST/Python runtime dependency:
``opt_lsst`` is only needed once, at build time.

Ported from salad's ``detect/lsst_cpp.py`` (``LSSTDetector``), without its
``detect.sh``/``detect_csv_to_table.py`` pipe (the binary's CSV and metadata
are parsed here directly) and without the ``Image`` row and ``Detector``
interface (a plain image ``uri`` in, an astropy ``Table`` out, like
``util/cutout.py``).
"""
import csv
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from astropy.table import Table

log = logging.getLogger(__name__)

ENGINE_DIR = Path(__file__).parent / "detect_engine"
BINARY = "detect_and_measure"

# the engine's CSV columns, with their types (see detect_engine/detect_and_measure.cc). ``mask`` is
# the footprint's own mask-plane union (not just its peak pixel's) -- deep-asteroids' own addition,
# so a detection sitting on a defect --exclude-mask-planes doesn't cover (e.g. CR/INTRP: a
# CR-interpolated bad column, #43) can still be filtered downstream instead of silently kept.
COLUMNS = {
    "polarity": str, "footprint_id": int, "footprint_area": int, "ix": int, "iy": int,
    "fx": float, "fy": float, "value": float, "significance": float,
    "ra_deg": float, "dec_deg": float, "flux": float, "flux_sigma": float, "snr": float,
    "log_likelihood": float, "mag": float, "mask": int,
}

# the engine's own default (SourceDetectionConfig's, matched exactly for fidelity -- see
# detect_common.h's Options::excludeMaskPlanes); CR/INTRP (cosmic-ray-interpolated pixels, e.g. a
# bad column) are deliberately not in it, since the reference algorithm doesn't exclude them either.
# A deeper/noisier search (lower --threshold) can still ask for them via --exclude-mask-planes
# explicitly instead (LSSTDetector's own exclude_mask_planes), rather than changing the engine's own
# default and losing that fidelity.
DEFAULT_EXCLUDE_MASK_PLANES = ("EDGE", "SAT", "SUSPECT", "BAD", "NO_DATA", "STREAK", "CROSSTALK", "NOT_DEBLENDED")


def parse_metadata(text):
    """``KEY=VALUE`` lines -> dict, values cast to int/float where they parse
    (``MJD`` keeps full double precision)."""
    meta = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        for cast in (int, float):
            try:
                value = cast(value)
                break
            except ValueError:
                continue
        meta[key] = value
    return meta


class LSSTDetector:
    """Runs the vendored ``detect_and_measure`` binary as a subprocess --
    needs it built in ``detect_engine/`` first (see ``VENDORED_FROM.md``)."""

    def __init__(self, engine_dir=ENGINE_DIR, threshold=5.0, polarity="positive", timeout=None,
                exclude_mask_planes=None):
        self.engine_dir = Path(engine_dir)
        self.threshold = threshold
        self.polarity = polarity
        self.timeout = timeout
        # None: don't pass --exclude-mask-planes at all, so the engine's own default (#43's
        # DEFAULT_EXCLUDE_MASK_PLANES) applies -- preserves default-threshold runs exactly.
        # Given explicitly (e.g. adding CR/INTRP for a deeper, noisier search -- #43), passed through
        # verbatim; () clears the engine's default entirely (equivalent to --exclude-mask-planes '').
        self.exclude_mask_planes = exclude_mask_planes
        if not (self.engine_dir / BINARY).exists():
            raise FileNotFoundError(
                f"{self.engine_dir / BINARY} not found -- build it first: "
                f"`source .../opt_lsst.sh && opt_lsst setup <tag> && bash {self.engine_dir}/build.sh` "
                f"(see {self.engine_dir}/VENDORED_FROM.md)"
            )

    def detect(self, uri):
        """Detect on the image at ``uri``. Returns an astropy ``Table`` with
        one row per peak (the engine's CSV columns, ``ra_deg``/``dec_deg``
        renamed ``ra``/``dec``) and the engine's exposure metadata (``MJD``,
        ``VISIT``, ``DETECTOR``, ...) in ``table.meta``."""
        with tempfile.TemporaryDirectory(prefix="deep-asteroids-detect-") as tmpdir:
            csv_path = Path(tmpdir) / "peaks.csv"
            meta_path = Path(tmpdir) / "peaks.meta"
            cmd = [
                str(self.engine_dir / BINARY), uri,
                "--sigma", str(self.threshold), "--threshold-polarity", self.polarity,
                "--csv", str(csv_path), "--metadata", str(meta_path), "--quiet",
            ]
            if self.exclude_mask_planes is not None:
                cmd += ["--exclude-mask-planes", ",".join(self.exclude_mask_planes)]
            log.info("running %s", " ".join(cmd))
            # one thread per process: callers parallelize across images
            env = dict(os.environ, OMP_NUM_THREADS="1")
            result = subprocess.run(
                cmd, check=False, timeout=self.timeout, capture_output=True, text=True, env=env)
            if result.returncode != 0:
                raise RuntimeError(
                    f"{BINARY} failed on {uri} (exit {result.returncode}):\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )
            with open(csv_path, newline="") as f:
                rows = list(csv.DictReader(f))
            meta = parse_metadata(meta_path.read_text())

        table = Table({
            name: np.array([cast(r[name]) for r in rows], dtype=cast) if rows else np.array([], dtype=cast)
            for name, cast in COLUMNS.items()
        })
        table.rename_column("ra_deg", "ra")
        table.rename_column("dec_deg", "dec")
        table.meta.update(meta)
        return table
