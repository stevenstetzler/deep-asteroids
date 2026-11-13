"""
Per-image detection catalogs for a search.

One ``<detector_exposure_id>.ecsv`` file per image in the catalog directory,
holding every peak the detector found plus a provenance header (engine
metadata, threshold, polarity, image uri). Catalogs are files, not database
rows: a survey's peaks are far too many to keep as rows (and the previous
run's were all deleted by ``/gscratch/scrubbed``'s purge, which is fine --
detection is deterministic, so a lost catalog is only a rerun). Keyed by
deep-db's ``detector_exposure.id``, so a search definition for each band of
one ``(night, field, detector)``, and the one spanning every band, share them.

Idempotent: a catalog that exists is skipped (``force`` redoes it). Files are
written to a temporary name and renamed, so an interrupted run never leaves a
half-written catalog behind.
"""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from deep_asteroids.database.images import Image

log = logging.getLogger(__name__)

PRODUCT_TYPE = "differenceExp"
CATALOG_NAME = re.compile(r"^\d+\.ecsv$")


@dataclass
class DetectSummary:
    n_written: int = 0
    n_skipped: int = 0
    n_detections: int = 0  # across the catalogs written (not the skipped ones)
    paths: list = field(default_factory=list)


def default_catalog_dir():
    """``/gscratch/scrubbed/$USER/deep_asteroids/catalogs`` -- a dedicated
    subdirectory of the user's scrubbed space, so removing catalogs can
    never touch anything else."""
    import getpass

    return Path("/gscratch/scrubbed") / getpass.getuser() / "deep_asteroids" / "catalogs"


def catalog_path(catalog_dir, deep_db_detector_exposure_id):
    return Path(catalog_dir) / f"{int(deep_db_detector_exposure_id)}.ecsv"


def allocated_cpus():
    """CPUs this process may use: ``$SLURM_CPUS_PER_TASK``, else
    ``$SLURM_CPUS_ON_NODE``, else 1 -- never ``nproc``."""
    for name in ("SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE"):
        value = os.environ.get(name)
        if value and value.isdigit():
            return max(1, int(value))
    return 1


def _write_catalog(table, path):
    tmp = path.with_name(path.name + ".tmp")
    table.write(tmp, format="ascii.ecsv", overwrite=True)
    os.replace(tmp, path)


def detect_exposures(session, search_definition, catalog_dir, detector, workers=None, force=False,
                     product_type=PRODUCT_TYPE):
    """Write a catalog for each of ``search_definition``'s exposures that
    lacks one. ``detector`` has ``detect(uri) -> Table`` (see
    ``util/detect.py``) and ``threshold``/``polarity`` attributes. Runs up to
    ``workers`` detections at once (default: the Slurm allocation). Raises if
    an exposure has no ``image`` row (run ``ingest images`` first)."""
    catalog_dir = Path(catalog_dir)
    catalog_dir.mkdir(parents=True, exist_ok=True)
    exposures = list(search_definition.exposures)
    ids = [e.deep_db_detector_exposure_id for e in exposures]
    images = {
        i.deep_db_detector_exposure_id: i
        for i in session.scalars(select(Image).where(
            Image.deep_db_detector_exposure_id.in_(ids), Image.product_type == product_type))
    }
    missing = sorted(set(ids) - set(images))
    if missing:
        raise ValueError(
            f"{search_definition.run_id}: no {product_type} image for detector-exposure(s) "
            f"{missing[:5]}{' ...' if len(missing) > 5 else ''} -- run `ingest images` first"
        )

    summary = DetectSummary(paths=[catalog_path(catalog_dir, i) for i in ids])
    todo = []
    for i, path in zip(ids, summary.paths):
        if path.exists() and not force:
            summary.n_skipped += 1
        else:
            todo.append((i, path))

    def run_one(item):
        detector_exposure_id, path = item
        image = images[detector_exposure_id]
        table = detector.detect(image.uri)
        table.meta.update({
            "deep_db_detector_exposure_id": detector_exposure_id, "image_uri": image.uri,
            "threshold": detector.threshold, "polarity": detector.polarity,
        })
        _write_catalog(table, path)
        return len(table)

    if todo:
        with ThreadPoolExecutor(max_workers=workers or allocated_cpus()) as pool:
            for n in pool.map(run_one, todo):
                summary.n_written += 1
                summary.n_detections += n
    return summary


def remove_catalogs(catalog_dir, paths):
    """Delete exactly ``paths`` (each must be a ``<number>.ecsv`` file
    directly inside ``catalog_dir``). Returns how many were removed. No
    globbing, no recursion, never the directory itself."""
    catalog_dir = Path(catalog_dir).resolve()
    removed = 0
    for path in paths:
        path = Path(path)
        if path.parent.resolve() != catalog_dir or not CATALOG_NAME.match(path.name):
            raise ValueError(f"refusing to remove {path}: not a catalog file in {catalog_dir}")
        if path.exists():
            path.unlink()
            removed += 1
    return removed
