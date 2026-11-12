"""
Forced PSF photometry and pixel cutouts at caller-supplied sky positions,
via the vendored ``cutout_and_photometry`` C++ engine (``cutout_engine/``
-- see that directory's ``VENDORED_FROM.md`` for provenance and how to
build it). No LSST/Python runtime dependency: ``opt_lsst`` is only needed
once, at build time.

Ported from salad's ``cutout/__init__.py`` (the ``CutoutPhotometryEngine``
interface) and ``cutout/lsst_cpp.py`` (``LSSTCutoutPhotometry``), combined
into one module here since deep-asteroids has only this one implementation
(salad's split anticipated other engines/surveys; not a concern yet here).
Adapted to take a plain image ``uri`` rather than a database row -- the
database model (``database/images.py``) was the only other candidate
caller, and this keeps the engine from depending on it at all.
"""
import abc
import csv
import logging
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

ENGINE_DIR = Path(__file__).parent / "cutout_engine"


@dataclass
class PhotometryRequest:
    """One position to measure. ``id`` is caller-defined (e.g. a
    tracklet's own id, as a string) and is echoed back unchanged on the
    matching ``PhotometryRecord``, purely so a caller can match results
    back up to whatever it asked for."""
    id: str
    ra: float  # deg
    dec: float  # deg


@dataclass
class PhotometryRecord:
    """One ``PhotometryRequest``'s result. ``in_bounds=False`` means the
    position fell outside the image (or its Wcs couldn't resolve the sky
    position to a pixel position at all) -- every other field is then
    ``None``. ``cutout_path`` is set only if the engine was asked to
    write cutouts and this position was in bounds."""
    id: str
    in_bounds: bool
    ra: float
    dec: float
    x: float | None = None
    y: float | None = None
    flux: float | None = None
    flux_sigma: float | None = None
    snr: float | None = None
    log_likelihood: float | None = None
    mag: float | None = None
    mag_err_lo: float | None = None
    mag_err_hi: float | None = None
    mask: int | None = None
    zero_point: float | None = None
    cutout_path: str | None = None


class CutoutPhotometryEngine(abc.ABC):
    """Measure forced photometry (and optionally cutouts) at a list of sky
    positions on one image. Subclasses implement ``measure()``."""

    @abc.abstractmethod
    def measure(self, uri: str, requests: Iterable[PhotometryRequest],
                cutout_dir: str | None = None, cutout_size: int = 51) -> Iterator[PhotometryRecord]:
        """Yield one ``PhotometryRecord`` per request, in the same order.
        Must not touch the database -- ``uri`` is the only thing read
        beyond ``requests`` itself."""
        raise NotImplementedError


def _float_or_none(s: str) -> float | None:
    if not s:
        return None
    v = float(s)
    return None if v != v else v  # NaN check ("nan" parses fine via float() but isn't a real value here)  # noqa: PLR0124


class LSSTCutoutPhotometry(CutoutPhotometryEngine):
    """Runs the vendored ``cutout_and_photometry`` binary (``cutout_engine/``)
    as a subprocess -- needs it built there first (see that directory's
    ``VENDORED_FROM.md``)."""

    def __init__(self, engine_dir: Path = ENGINE_DIR, timeout: float | None = None):
        self.engine_dir = Path(engine_dir)
        self.timeout = timeout

        binary_path = self.engine_dir / "cutout_and_photometry"
        if not binary_path.exists():
            raise FileNotFoundError(
                f"{binary_path} not found -- build it first: "
                f"`source .../opt_lsst.sh && opt_lsst setup <tag> && bash {self.engine_dir}/build.sh` "
                f"(see {self.engine_dir}/VENDORED_FROM.md)"
            )

    def measure(self, uri: str, requests: Iterable[PhotometryRequest],
                cutout_dir: str | None = None, cutout_size: int = 51) -> Iterator[PhotometryRecord]:
        import subprocess

        requests = list(requests)
        if cutout_dir is not None:
            Path(cutout_dir).mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="deep-asteroids-cutout-") as tmpdir:
            positions_path = Path(tmpdir) / "positions.csv"
            out_path = Path(tmpdir) / "phot.csv"

            with open(positions_path, "w", newline="") as f:
                writer = csv.writer(f)
                for r in requests:
                    writer.writerow([r.id, r.ra, r.dec])

            cmd = [
                str(self.engine_dir / "cutout_and_photometry"),
                uri,
                "--positions", str(positions_path),
                "--csv", str(out_path),
                "--quiet",
            ]
            if cutout_dir is not None:
                cmd += ["--cutout-dir", str(cutout_dir), "--cutout-size", str(cutout_size)]

            log.info("running %s", " ".join(cmd))
            result = subprocess.run(cmd, check=False, timeout=self.timeout, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"cutout_and_photometry failed on {uri} (exit {result.returncode}):\n"
                    f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                )

            with open(out_path, newline="") as f:
                for row in csv.DictReader(f):
                    in_bounds = row["in_bounds"] == "1"
                    cutout_path = None
                    if in_bounds and cutout_dir is not None and row["cutout_written"] == "1":
                        cutout_path = str(Path(cutout_dir) / f"{row['id']}.fits")
                    # every field below the position is meaningless (and
                    # NaN/0 in the CSV, not blank) for an out-of-bounds
                    # position -- collapse it to None rather than leaking
                    # that CSV-level "no value" convention out to callers,
                    # matching PhotometryRecord's own docstring.
                    yield PhotometryRecord(
                        id=row["id"],
                        in_bounds=in_bounds,
                        ra=float(row["ra_deg"]),
                        dec=float(row["dec_deg"]),
                        x=_float_or_none(row["x"]) if in_bounds else None,
                        y=_float_or_none(row["y"]) if in_bounds else None,
                        flux=_float_or_none(row["flux"]) if in_bounds else None,
                        flux_sigma=_float_or_none(row["flux_sigma"]) if in_bounds else None,
                        snr=_float_or_none(row["snr"]) if in_bounds else None,
                        log_likelihood=_float_or_none(row["log_likelihood"]) if in_bounds else None,
                        mag=_float_or_none(row["mag"]) if in_bounds else None,
                        mag_err_lo=_float_or_none(row["mag_err_lo"]) if in_bounds else None,
                        mag_err_hi=_float_or_none(row["mag_err_hi"]) if in_bounds else None,
                        mask=int(row["mask"]) if in_bounds else None,
                        zero_point=_float_or_none(row["zero_point"]) if in_bounds else None,
                        cutout_path=cutout_path,
                    )
