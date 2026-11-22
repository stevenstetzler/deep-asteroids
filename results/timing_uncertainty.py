"""
Timing uncertainty against numbered known objects (#46).

How sensitive the positional agreement of `astrometric_uncertainty.py` is to a timing error in the
known objects' ephemerides: for a grid of offsets applied to the known tracklet's evaluation time
(default [0, 3]s, 0.05s steps -- 61 points), redoes the detection-level positional comparison (the
same numbered-object matches, the same 1" detection radius) and logs the median and standard
deviation of the kept dRA/dDec at that offset, to a CSV and a 1D plot (offset on the x-axis; dRA in
C0, dDec in C1).

The crossmatch pairs and their gathered detections are fetched once and reused across every grid
point, so the whole scan costs about one `astrometric_uncertainty.py` run, not 41 of them.

Usage (compute node; reads the full-survey database, read-only):

    DEEP_ASTEROIDS_DB=sqlite:////path/to/deep_asteroids_full_survey.sqlite \
        python results/timing_uncertainty.py --band VR \
        --out-csv timing_uncertainty.csv --out-plot timing_uncertainty.png
"""
import argparse

import numpy as np
from sqlalchemy.orm import Session

from deep_asteroids.crossmatch.uncertainty import (
    DEFAULT_DETECTION_RADIUS_ARCSEC,
    DEFAULT_MATCH_RADIUS_ARCSEC,
    time_offset_scan,
    write_time_offset_csv,
)
from deep_asteroids.database import get_db
from deep_asteroids.database.search import parse_band

DEFAULT_MIN_OFFSET_SECONDS = 0.0
DEFAULT_MAX_OFFSET_SECONDS = 3.0
DEFAULT_STEP_SECONDS = 0.05


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-csv", help="CSV to write, one row per timing offset")
    p.add_argument("--out-plot", help="PNG: median +- std of dRA/dDec against the timing offset")
    p.add_argument("--min-offset-seconds", type=float, default=DEFAULT_MIN_OFFSET_SECONDS)
    p.add_argument("--max-offset-seconds", type=float, default=DEFAULT_MAX_OFFSET_SECONDS)
    p.add_argument("--step-seconds", type=float, default=DEFAULT_STEP_SECONDS)
    p.add_argument("--match-radius-arcsec", type=float, default=DEFAULT_MATCH_RADIUS_ARCSEC,
                   help="A result is matched to a numbered known object below this mean separation")
    p.add_argument("--detection-radius-arcsec", type=float, default=DEFAULT_DETECTION_RADIUS_ARCSEC,
                   help="A gathered detection counts only within this separation of the predicted position")
    p.add_argument("--night", type=int, action="append", help="Only this night (repeatable)")
    p.add_argument("--run-id", action="append", help="Only this search run (repeatable)")
    p.add_argument("--band", default=None,
                   help="Only searches of this band (e.g. VR), or '' for the undefined-band (legacy) searches; "
                        "default: every band")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # +epsilon so max_offset_seconds itself is included despite float step accumulation
    offsets = np.arange(args.min_offset_seconds, args.max_offset_seconds + args.step_seconds / 2, args.step_seconds)
    with Session(get_db()) as session:
        rows = time_offset_scan(
            session, offsets, nights=args.night, run_ids=args.run_id, match_radius_arcsec=args.match_radius_arcsec,
            detection_radius_arcsec=args.detection_radius_arcsec, band=parse_band(args.band))
    if not rows or not any(r.n for r in rows):
        raise SystemExit("no detections of numbered known objects matched (is the crossmatch done?)")
    print(f"{len(rows)} offset(s), {rows[len(rows) // 2].n:,} detections matched at offset "
          f"{rows[len(rows) // 2].offset_seconds:g}s")
    for r in rows:
        print(f"  {r.offset_seconds:+.2f}s: n={r.n:,} dRA median {r.median_dra_arcsec:+.4f}\" std {r.std_dra_arcsec:.4f}\" "
              f"dDec median {r.median_ddec_arcsec:+.4f}\" std {r.std_ddec_arcsec:.4f}\"")

    if args.out_csv:
        print(f"wrote {write_time_offset_csv(rows, args.out_csv)}")
    if args.out_plot:
        from deep_asteroids.viz.plots import plot_time_offset_scan

        print(f"wrote {plot_time_offset_scan(rows, args.out_plot)}")


if __name__ == "__main__":
    main()
