"""
Positional uncertainty against numbered known objects (#46).

For every numbered object (``permid`` set) a result matched within 2" (the submitted/known
crossmatch radius), compares that object's own linear fit against the matching result's gathered
detections within 1" of the predicted position, as dRA/dDec residuals (observed - predicted).
Writes a per-detection CSV and two plots: a histogram of the residuals, and the residuals against
each object's own median magnitude (with per-magnitude-bin median/sigma and density contours).

``--timing-offset-seconds`` shifts the known tracklet's evaluation time by a fixed amount before
comparing -- a quick single-offset check; `timing_uncertainty.py` scans a grid of offsets instead.

Usage (compute node; reads the full-survey database, read-only):

    DEEP_ASTEROIDS_DB=sqlite:////path/to/deep_asteroids_full_survey.sqlite \
        python results/astrometric_uncertainty.py --band VR \
        --out-csv astrometric_residuals.csv --out-hist astrometric_residuals_hist.png \
        --out-vs-mag astrometric_residuals_vs_mag.png

Takes under a minute over the VR full-survey results.
"""
import argparse

from sqlalchemy.orm import Session

from deep_asteroids.crossmatch.uncertainty import (
    DEFAULT_DETECTION_RADIUS_ARCSEC,
    DEFAULT_MATCH_RADIUS_ARCSEC,
    median_mag_by_object,
    position_residuals,
    write_residuals_csv,
)
from deep_asteroids.database import get_db
from deep_asteroids.database.search import parse_band


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out-csv", help="CSV to write, one row per detection")
    p.add_argument("--out-hist", help="PNG: histograms of dRA/dDec")
    p.add_argument("--out-vs-mag", help="PNG: dRA/dDec against each object's median magnitude")
    p.add_argument("--timing-offset-seconds", type=float, default=0.0,
                   help="Shift the known tracklet's evaluation time by this many seconds before comparing")
    p.add_argument("--match-radius-arcsec", type=float, default=DEFAULT_MATCH_RADIUS_ARCSEC,
                   help="A result is matched to a numbered known object below this mean separation")
    p.add_argument("--detection-radius-arcsec", type=float, default=DEFAULT_DETECTION_RADIUS_ARCSEC,
                   help="A gathered detection counts only within this separation of the predicted position")
    p.add_argument("--mag-bin-width", type=float, default=0.5, help="Magnitude bin width for the vs-mag plot")
    p.add_argument("--mag-min-n", type=int, default=20,
                   help="A magnitude bin needs at least this many detections for a median/sigma line")
    p.add_argument("--night", type=int, action="append", help="Only this night (repeatable)")
    p.add_argument("--run-id", action="append", help="Only this search run (repeatable)")
    p.add_argument("--band", default=None,
                   help="Only searches of this band (e.g. VR), or '' for the undefined-band (legacy) searches; "
                        "default: every band")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    with Session(get_db()) as session:
        rows = position_residuals(
            session, nights=args.night, run_ids=args.run_id, match_radius_arcsec=args.match_radius_arcsec,
            detection_radius_arcsec=args.detection_radius_arcsec, band=parse_band(args.band),
            time_offset_seconds=args.timing_offset_seconds)
    if not rows:
        raise SystemExit("no detections of numbered known objects matched (is the crossmatch done?)")
    n_objects = len({r.known_tracklet_id for r in rows})
    print(f"{len(rows):,} detections of {n_objects:,} numbered known object(s)")

    if args.out_csv:
        print(f"wrote {write_residuals_csv(rows, args.out_csv)}")
    if args.out_hist:
        from deep_asteroids.viz.plots import plot_position_residuals_hist

        print(f"wrote {plot_position_residuals_hist(rows, args.out_hist)}")
    if args.out_vs_mag:
        from deep_asteroids.viz.plots import plot_position_residuals_vs_mag

        by_object = median_mag_by_object(rows)
        print(f"wrote {plot_position_residuals_vs_mag(rows, args.out_vs_mag, by_object, args.mag_bin_width, args.mag_min_n)}")


if __name__ == "__main__":
    main()
