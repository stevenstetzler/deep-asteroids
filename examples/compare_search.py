"""Compare two ingested find-asteroids runs, level by level, from the most to
the least deterministic -- the first level that differs says where a
difference enters (detector, catalog assembly, Hough search, refinement).

usage: compare_search.py NEW_RUN_ID OLD_RUN_ID [--new-db URL] [--old-db URL] [--catalog-dir DIR]

Both runs are read from ``$DEEP_ASTEROIDS_DB`` unless a ``--*-db`` URL says
otherwise (e.g. a copy of the database holding a rerun with another seed, to
measure the refinement stage's noise floor).

  L0 detector     every distinct old `gathered` point against the new catalog
                  (needs --catalog-dir: the catalogs must still exist)
  L1 Hough        per result_num: x, y, direction, n
  L2 refinement   per result_num: the tracklet and n1/n2/n5/n10
  L3 outputs      per result_num: the gathered and points sets

The two runs may label their exposures with different times (salad's earlier
searches labelled each exposure a fixed 0.5 s after deep-db's midpoint). That
offset is not assumed: it is measured, as the median difference between the
two runs' gathered times (results with the same number of exposures, matched
in time order), reported, and removed from the old run's times and tracklets
before anything is compared.

Reports counts and the size of the differences, not a pass/fail.
"""
import argparse
import os

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from find_asteroids.models import Gathered, Points, Result, Search, Tracklet

HOUGH = ("x", "y", "direction", "n")
COUNTS = ("n1", "n2", "n5", "n10")
MATERIAL_ARCSEC = 0.05  # a tracklet difference smaller than this is invisible next to a pixel (0.26")
TRACKLET = ("vra", "vdec", "ra_0", "dec_0", "tref", "ra_ref", "dec_ref", "tmin", "tmax",
            "sigma_vra", "sigma_vdec", "sigma_t")


def load_run(url, run_id):
    """{result_num: {..result columns.., 'tracklet': {...}, 'gathered': set, 'points': set}} for a run."""
    engine = create_engine(url)
    out = {}
    with Session(engine) as session:
        rows = session.execute(
            select(Result, Tracklet).join(Search, Search.id == Result.search_id)
            .join(Tracklet, Tracklet.result_id == Result.id).where(Search.run_id == run_id)).all()
        if not rows:
            raise SystemExit(f"{run_id}: no results in {url}")
        for result, tracklet in rows:
            d = {k: getattr(result, k) for k in HOUGH + COUNTS}
            d["tracklet"] = {k: getattr(tracklet, k) for k in TRACKLET}
            pts = session.execute(
                select(Gathered.time, Gathered.ra, Gathered.dec, Gathered.flux, Gathered.mag)
                .where(Gathered.result_id == result.id).order_by(Gathered.id)).all()
            d["gathered_rows"] = [tuple(r) for r in pts]
            d["gathered"] = {(round(t, 6), round(ra, 9), round(dec, 9)) for t, ra, dec, _, _ in pts}
            pts = session.execute(
                select(Points.time, Points.ra, Points.dec).where(Points.result_id == result.id)).all()
            d["points_rows"] = [tuple(r) for r in pts]
            d["points"] = {(round(t, 6), round(ra, 9), round(dec, 9)) for t, ra, dec in pts}
            out[result.result_num] = d
    engine.dispose()
    return out


def measure_time_shift(new, old):
    """``(shift_days, n_results, spread_seconds)``: how much later the old run's time labels are
    than the new run's, from results whose gathered points cover the same number of distinct
    times (matched in time order). ``(0, 0, 0)`` if no result is comparable."""
    diffs = []
    for k in sorted(set(new) & set(old)):
        nt = sorted({round(r[0], 9) for r in new[k]["gathered_rows"]})
        ot = sorted({round(r[0], 9) for r in old[k]["gathered_rows"]})
        if nt and len(nt) == len(ot):
            diffs.append(np.array(ot) - np.array(nt))
    if not diffs:
        return 0.0, 0, 0.0
    alld = np.concatenate(diffs)
    shift = float(np.median(alld))
    return shift, len(diffs), float(np.abs(alld - shift).max() * 86400)


SNAP_DAYS = 1e-6  # 86 ms: exposures are minutes apart, so this can only match an exposure to itself


def snap(times, t):
    """The entry of the sorted array ``times`` nearest ``t`` if within ``SNAP_DAYS``, else ``t``."""
    i = int(np.searchsorted(times, t))
    best = min((times[j] for j in (i - 1, i) if 0 <= j < len(times)), key=lambda x: abs(x - t), default=t)
    return float(best) if abs(best - t) < SNAP_DAYS else t


def run_times(run):
    """The sorted distinct gathered/points times of a run."""
    return np.array(sorted({r[0] for d in run.values() for r in d["gathered_rows"] + d["points_rows"]}))


def align_old(old, shift, times):
    """Re-express the old run in the new run's time labels (``t_new = t_old - shift``), snapping each
    time onto the new run's own (``times``) so equal exposures compare equal exactly."""
    if shift == 0:
        return
    for r in old.values():
        t = r["tracklet"]
        # position(t_new) = ra_0 + vra * (t_new + shift), so only the MJD-0 intercept and the times move
        t["ra_0"] += t["vra"] * shift
        t["dec_0"] += t["vdec"] * shift
        for key in ("tref", "tmin", "tmax"):
            if t[key] is not None:
                t[key] -= shift
        r["gathered_rows"] = [(snap(times, tt - shift), *rest) for tt, *rest in r["gathered_rows"]]
        r["gathered"] = {(round(tt, 6), round(ra, 9), round(dec, 9)) for tt, ra, dec, _, _ in r["gathered_rows"]}
        r["points_rows"] = [(snap(times, tt - shift), ra, dec) for tt, ra, dec in r["points_rows"]]
        r["points"] = {(round(tt, 6), round(ra, 9), round(dec, 9)) for tt, ra, dec in r["points_rows"]}


def arcsec(ra1, dec1, ra2, dec2):
    dra = (np.asarray(ra1) - ra2 + 180) % 360 - 180  # wrapped: ra_0 is stored mod 360
    return np.hypot(dra * np.cos(np.radians(dec1)), np.asarray(dec1) - dec2) * 3600


def level0(new_run_id, old, url, catalog_dir):
    from sqlalchemy import select as sel

    from deep_asteroids.database.search import SearchDefinition
    from deep_asteroids.search.catalog import build_catalog

    with Session(create_engine(url)) as session:
        sd = session.scalars(sel(SearchDefinition).where(SearchDefinition.run_id == new_run_id)).one()
        _ = list(sd.exposures)
        cat = build_catalog(sd, catalog_dir)
    t = cat.table
    new_ra, new_dec = np.asarray(t["ra"], float), np.asarray(t["dec"], float)  # degrees
    new_flux = np.asarray(t["flux"], float) if "flux" in t.colnames else np.full(len(t), np.nan)
    new_mag = np.asarray(t["mag"], float) if "mag" in t.colnames else np.full(len(t), np.nan)
    by_time = {}  # detections by exposure, keyed by the exposure's TAI time (what the old gathered rows carry)
    for i, tt in enumerate(t["time"].tai.mjd):
        by_time.setdefault(float(tt), []).append(i)
    exposure_times = np.array(sorted(by_time))

    old_points = {}  # the distinct old gathered points
    for r in old.values():
        for time, ra, dec, flux, mag in r["gathered_rows"]:
            old_points[(round(time, 9), round(ra, 9), round(dec, 9))] = (time, ra, dec, flux, mag)
    seps, dflux, dmag, missing = [], [], [], 0
    for time, ra, dec, flux, mag in old_points.values():
        idx = by_time.get(snap(exposure_times, float(time)))
        if not idx:
            missing += 1
            continue
        s = arcsec(new_ra[idx], new_dec[idx], ra, dec)
        best = idx[int(np.argmin(s))]
        seps.append(s.min())
        if flux is not None:
            dflux.append(abs(new_flux[best] - flux))
        if mag is not None:
            dmag.append(abs(new_mag[best] - mag))
    seps = np.array(seps)
    print("L0 detector (old gathered points vs the new catalog)")
    print(f"   {len(old_points):,} distinct old points; {len(t):,} new detections in "
          f"{len([c for c in cat.n_per_exposure if c])} exposure(s); no matching exposure: {missing}")
    if len(seps):
        print(f"   nearest new detection: exact (< 1e-6\"): {(seps < 1e-6).sum():,}, within 0.05\": "
              f"{(seps < 0.05).sum():,}, beyond: {(seps >= 0.05).sum():,}; max {seps.max():.4g}\"")
    if dflux:
        print(f"   flux/mag of that detection: max |dflux| {np.nanmax(dflux):.3g}, "
              f"max |dmag| {np.nanmax(dmag) if dmag else float('nan'):.3g}")


def level1(new, old):
    nums = sorted(set(new) & set(old))
    print("L1 Hough search (x, y, direction, n)")
    print(f"   results: new {len(new)}, old {len(old)}, in common {len(nums)}")
    same = sum(all(new[k][h] == old[k][h] for h in HOUGH) for k in nums)
    print(f"   identical (x, y, direction, n): {same}/{len(nums)}")
    for h in HOUGH:
        diff = [abs(new[k][h] - old[k][h]) for k in nums]
        if any(diff):
            print(f"   {h}: differs in {sum(d > 0 for d in diff)} result(s), max |diff| {max(diff)}")


def level2(new, old):
    nums = sorted(set(new) & set(old))
    print("L2 refinement (tracklet, n1/n2/n5/n10)")
    print(f"   identical n1/n2/n5/n10: {sum(all(new[k][c] == old[k][c] for c in COUNTS) for k in nums)}/{len(nums)}")
    for c in COUNTS:
        diff = [abs(new[k][c] - old[k][c]) for k in nums]
        if any(diff):
            print(f"   {c}: differs in {sum(d > 0 for d in diff)}, max |diff| {max(diff)}")
    exact = sum(all(new[k]["tracklet"][c] == old[k]["tracklet"][c] for c in TRACKLET) for k in nums)
    print(f"   bit-identical tracklets: {exact}/{len(nums)}")
    for c in ("vra", "vdec", "ra_0", "dec_0", "ra_ref", "dec_ref", "tref"):
        diff = np.array([abs(new[k]["tracklet"][c] - old[k]["tracklet"][c]) for k in nums], float)
        diff = diff[~np.isnan(diff)]
        if len(diff) and diff.max() > 0:
            print(f"   {c}: median |diff| {np.median(diff):.3g}, max {diff.max():.3g}")
    # position difference of the tracklets over the old run's span, the number that matters on the sky
    pos = []
    for k in nums:
        n_, o_ = new[k]["tracklet"], old[k]["tracklet"]
        for tt in np.linspace(o_["tmin"], o_["tmax"], 5):
            pos.append(arcsec(n_["ra_0"] + n_["vra"] * tt, n_["dec_0"] + n_["vdec"] * tt,
                              o_["ra_0"] + o_["vra"] * tt, o_["dec_0"] + o_["vdec"] * tt))
    if pos:
        pos = np.array(pos)
        worst = pos.reshape(len(nums), 5).max(axis=1)
        material = [k for k, w in zip(nums, worst) if w > MATERIAL_ARCSEC]
        print(f"   results whose predicted position differs by more than {MATERIAL_ARCSEC}\" anywhere in the span: "
              f"{len(material)}/{len(nums)}" + (f"  (result_num {material})" if 0 < len(material) <= 12 else ""))
        print(f"   predicted-position difference over the span (5 times per result): "
              f"median {np.median(pos):.3g}\", 99th pct {np.percentile(pos, 99):.3g}\", max {pos.max():.3g}\"")


def level3(new, old):
    nums = sorted(set(new) & set(old))
    print("L3 outputs (gathered and points sets)")
    for name in ("gathered", "points"):
        jac, same = [], 0
        for k in nums:
            a, b = new[k][name], old[k][name]
            union = len(a | b)
            jac.append(len(a & b) / union if union else 1.0)
            same += a == b
        print(f"   {name}: identical sets {same}/{len(nums)}; Jaccard min {min(jac):.4f}, median {np.median(jac):.4f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("new_run_id")
    parser.add_argument("old_run_id")
    parser.add_argument("--new-db", default=os.environ.get("DEEP_ASTEROIDS_DB"))
    parser.add_argument("--old-db", default=os.environ.get("DEEP_ASTEROIDS_DB"))
    parser.add_argument("--catalog-dir", help="enables L0: the new run's detection catalogs")
    args = parser.parse_args()
    if not (args.new_db and args.old_db):
        parser.error("set $DEEP_ASTEROIDS_DB or give --new-db/--old-db")

    new, old = load_run(args.new_db, args.new_run_id), load_run(args.old_db, args.old_run_id)
    print(f"new: {args.new_run_id}   old: {args.old_run_id}")
    shift, n, spread = measure_time_shift(new, old)
    if shift == 0:
        print("time labels: identical" if n else "time labels: not comparable, assumed identical")
    else:
        print(f"time labels: the old run's are {shift * 86400:+.3f} s from the new run's (median over {n} results, "
              f"spread {spread * 1e3:.2f} ms); removed from the old run before comparing")
    align_old(old, shift, run_times(new))
    if args.catalog_dir:
        level0(args.new_run_id, old, args.new_db, args.catalog_dir)
    level1(new, old)
    level2(new, old)
    level3(new, old)


if __name__ == "__main__":
    main()
