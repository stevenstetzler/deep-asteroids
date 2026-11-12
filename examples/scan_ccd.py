"""Score every result of one search run: forced photometry along each result's
tracklet in every exposure, combined into an inverse-variance-weighted stacked
flux SNR. Renders light curve/coadd/movie for the top --top results.
usage: scan_ccd.py RUN_ID OUT_DIR TMP_DIR [--top N]"""
import json, math, shutil, sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from deep_asteroids.database import get_db
from deep_asteroids.database.images import Image
from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.util.cutout import LSSTCutoutPhotometry, PhotometryRequest
from deep_asteroids.viz import tracklet as viz
from find_asteroids.models import Result, Search, Tracklet

run_id, out, tmp = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 3
only = [int(x) for x in sys.argv[sys.argv.index("--only") + 1].split(",")] if "--only" in sys.argv else None
OFFSET = 32.184 / 86400  # salad times are early (README)

with Session(get_db()) as s:
    sd = s.scalars(select(SearchDefinition).where(SearchDefinition.run_id == run_id)).one()
    rows = s.execute(select(Result.id, Result.result_num, Result.n, Tracklet)
                     .join(Search).join(Tracklet, Tracklet.result_id == Result.id)
                     .where(Search.run_id == run_id).order_by(Result.result_num)).all()
    if only is not None:
        rows = [r for r in rows if r[1] in only]
    images = {i.deep_db_detector_exposure_id: i for i in s.scalars(select(Image).where(
        Image.product_type == "differenceExp",
        Image.deep_db_detector_exposure_id.in_([e.deep_db_detector_exposure_id for e in sd.exposures])))}
    s.expunge_all()

engine = LSSTCutoutPhotometry()
meas = {r[1]: [] for r in rows}  # result_num -> [TrackletMeasurement]
recs = {r[1]: [] for r in rows}
for i, e in enumerate(sd.exposures):
    img = images.get(e.deep_db_detector_exposure_id)
    if img is None:
        continue
    tt = e.midpoint_mjd - OFFSET
    reqs, pos = [], {}
    for _, num, _, t in rows:
        if not t.tmin - 0.01 <= tt <= t.tmax + 0.01:
            continue
        ra, dec = (t.ra_0 + t.vra * tt) % 360, t.dec_0 + t.vdec * tt
        reqs.append(PhotometryRequest(id=f"r{num:03d}_{i:04d}", ra=ra, dec=dec))
        pos[num] = (ra, dec)
    for rec, num in zip(engine.measure(img.uri, reqs, cutout_dir=str(tmp), cutout_size=viz.DEFAULT_CUTOUT_SIZE), pos):
        recs[num].append(rec)
        meas[num].append(viz.TrackletMeasurement(
            deep_db_detector_exposure_id=e.deep_db_detector_exposure_id, mjd_tai=tt, ra=pos[num][0], dec=pos[num][1],
            in_bounds=rec.in_bounds, night=img.night, field=img.field, expnum=img.expnum, mag=rec.mag,
            mag_err_lo=rec.mag_err_lo, mag_err_hi=rec.mag_err_hi, cutout_path=rec.cutout_path))

def score(rs):
    v = [(r.flux, r.flux_sigma) for r in rs if r.in_bounds and r.flux is not None and r.flux_sigma and r.flux_sigma > 0
         and math.isfinite(r.flux)]
    if not v:
        return 0, 0.0, 0.0
    w = sum(1 / sg**2 for _, sg in v)
    mean = sum(f / sg**2 for f, sg in v) / w
    return len(v), mean, mean * math.sqrt(w)  # n, weighted mean flux, stacked SNR

stats = []
for _, num, n, t in rows:
    k, mean, snr = score(recs[num])
    stats.append({"result_num": num, "n_points": n, "n_meas": k, "mean_flux": mean, "stack_snr": snr})
stats.sort(key=lambda d: -d["stack_snr"])
out.mkdir(parents=True, exist_ok=True)
(out / "scores.json").write_text(json.dumps(stats, indent=1))
for d in stats:
    print(f"#{d['result_num']:>2} n={d['n_points']:>3} meas={d['n_meas']:>3} mean_flux={d['mean_flux']:9.1f} stack_snr={d['stack_snr']:7.2f}")

from astropy.io import fits
import collections
for d in (stats if only is not None else stats[:top]):
    num = d["result_num"]
    sub = out / f"result{num}"
    for m in meas[num]:  # keep only this result's cutouts
        if m.cutout_path:
            sub.mkdir(parents=True, exist_ok=True)
            m.cutout_path = str(shutil.copy(m.cutout_path, sub))
    # cutouts clipped by a chip edge have a different shape and can't be stacked: keep the modal shape
    shapes = [(m, fits.getdata(m.cutout_path).shape) for m in meas[num] if m.cutout_path]
    modal = collections.Counter(sh for _, sh in shapes).most_common(1)[0][0]
    for m, sh in shapes:
        if sh != modal:
            m.cutout_path = None
    summ = viz.VizSummary(tracklet=SimpleNamespace(source="find-asteroids", night=sd.night, component=None,
                          orbit_id=None, trkid=None, provid=f"{run_id} #{num}"), measurements=meas[num])
    viz.render_light_curve_plot(summ, sub); viz.render_coadd_plot(summ, sub)
    viz.render_movie(summ, sub, annotate="{night}/{field}/{expnum}")
    for f in sub.glob("*.fits"):
        f.unlink()
