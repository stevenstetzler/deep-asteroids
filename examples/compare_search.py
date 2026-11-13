"""Compare two ingested find-asteroids runs, level by level, from the most to
the least deterministic -- the first level that differs says where a
difference enters (detector, catalog assembly, Hough search, refinement).

usage: compare_search.py NEW_RUN_ID OLD_RUN_ID [--new-db URL] [--old-db URL]
                         [--catalog-dir DIR --shutter-offset-seconds S]

Both runs are read from ``$DEEP_ASTEROIDS_DB`` unless a ``--*-db`` URL says
otherwise (e.g. a copy of the database holding a rerun with another seed, to
measure the refinement stage's noise floor).

  L0 detector     every distinct old `gathered` point against the new catalog
                  (needs --catalog-dir: the catalogs must still exist)
  L1 Hough        per result_num: x, y, direction, n
  L2 refinement   per result_num: the tracklet and n1/n2/n5/n10
  L3 outputs      per result_num: the gathered and points sets

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
            d["points"] = {(round(t, 6), round(ra, 9), round(dec, 9)) for t, ra, dec in pts}
            out[result.result_num] = d
    engine.dispose()
    return out


def arcsec(ra1, dec1, ra2, dec2):
    dra = (np.asarray(ra1) - ra2 + 180) % 360 - 180  # wrapped: ra_0 is stored mod 360
    return np.hypot(dra * np.cos(np.radians(dec1)), np.asarray(dec1) - dec2) * 3600


def level0(new_run_id, old, url, catalog_dir, shutter_offset_seconds):
    from sqlalchemy import select as sel

    from deep_asteroids.database.search import SearchDefinition
    from deep_asteroids.search.catalog import build_catalog

    with Session(create_engine(url)) as session:
        sd = session.scalars(sel(SearchDefinition).where(SearchDefinition.run_id == new_run_id)).one()
        _ = list(sd.exposures)
        cat = build_catalog(sd, catalog_dir, shutter_offset_seconds=shutter_offset_seconds)
    t = cat.table
    new_ra, new_dec = np.asarray(t["ra"], float), np.asarray(t["dec"], float)  # degrees
    new_flux = np.asarray(t["flux"], float) if "flux" in t.colnames else np.full(len(t), np.nan)
    new_mag = np.asarray(t["mag"], float) if "mag" in t.colnames else np.full(len(t), np.nan)
    by_time = {}  # detections by exposure, keyed by the exposure's TAI time (what the old gathered rows carry)
    for i, tt in enumerate(t["time"].tai.mjd):
        by_time.setdefault(round(float(tt), 6), []).append(i)

    old_points = {}  # the distinct old gathered points
    for r in old.values():
        for time, ra, dec, flux, mag in r["gathered_rows"]:
            old_points[(round(time, 9), round(ra, 9), round(dec, 9))] = (time, ra, dec, flux, mag)
    seps, dflux, dmag, missing = [], [], [], 0
    for time, ra, dec, flux, mag in old_points.values():
        idx = by_time.get(round(float(time), 6))
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
    print(f"   engine time label vs deep-db midpoint + offset: max {cat.max_time_discrepancy_seconds * 1e3:.3f} ms")


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
    parser.add_argument("--shutter-offset-seconds", type=float, default=0.0)
    args = parser.parse_args()
    if not (args.new_db and args.old_db):
        parser.error("set $DEEP_ASTEROIDS_DB or give --new-db/--old-db")

    new, old = load_run(args.new_db, args.new_run_id), load_run(args.old_db, args.old_run_id)
    print(f"new: {args.new_run_id}   old: {args.old_run_id}")
    if args.catalog_dir:
        level0(args.new_run_id, old, args.new_db, args.catalog_dir, args.shutter_offset_seconds)
    level1(new, old)
    level2(new, old)
    level3(new, old)


if __name__ == "__main__":
    main()
