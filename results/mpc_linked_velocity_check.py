"""Why were submitted (mpc_linked) tracklets missed? Out of velocity range, or in range and still missed?

For every mpc_linked known tracklet whose fitted track crosses a searched chip for at least half of that
search's exposures, ignoring speed (the geometry of crossmatch/completeness.py), record whether its speed is
inside any such search's velocity range and whether a result matched it within the submitted radius (2").
With a band, only that band's searches are considered, and only a result of one of them counts as a match
(``VR``; ``none`` for the undefined-band legacy searches; omitted: every search).

Usage (compute node; reads the full-survey database, read-only):

    DEEP_ASTEROIDS_DB=sqlite:////path/to/deep_asteroids_full_survey.sqlite \
        python results/mpc_linked_velocity_check.py mpc_velocity.csv [BAND]

Takes about an hour over the survey. Columns: known_tracklet_id, night, speed, mag (empty for mpc_linked),
best_run_id (the chip with the most exposures, preferring an in-range search), n_on_chip, n_exposures, v0, v1,
in_range_any, recovered. vmin_all/vmax_all are only approximate (they restart whenever the best chip changes);
do not rely on them. The missed in-range tracklets are in_range_any == 1 and recovered == 0.
"""
import collections, sys
import numpy as np
from find_asteroids.models import Result, Search
from sqlalchemy import select
from sqlalchemy.orm import Session
from deep_asteroids.crossmatch.completeness import inside_quadrilaterals
from deep_asteroids.crossmatch.geometry import exposure_times_tai, positions
from deep_asteroids.database import get_db
from deep_asteroids.database.crossmatch import CrossmatchCandidate
from deep_asteroids.database.search import SearchDefinition, SearchDefinitionExposure, band_clause, parse_band
from deep_asteroids.database.tracklets import KnownTracklet

RADIUS = 2.0  # --submitted-radius-arcsec default
BAND = parse_band(sys.argv[2] if len(sys.argv) > 2 else None)
IN_BAND = None if BAND is Ellipsis else select(Result.id).where(Result.search_id.in_(
    select(Search.id).where(Search.run_id.in_(select(SearchDefinition.run_id).where(band_clause(BAND))))))
out = open(sys.argv[1], "w")
out.write("known_tracklet_id,night,speed,mag,best_run_id,n_on_chip,n_exposures,v0,v1,in_range_any,vmin_all,vmax_all,recovered\n")
with Session(get_db()) as session:
    runs = session.execute(select(SearchDefinition.id, SearchDefinition.run_id, SearchDefinition.night,
                                  Search.velocity_0, Search.velocity_1)
                           .join(Search, Search.run_id == SearchDefinition.run_id)
                           .where(Search.id.in_(select(Result.search_id)),
                                  *([] if BAND is Ellipsis else [band_clause(BAND)]))
                           .order_by(SearchDefinition.run_id)).all()
    by_night = collections.defaultdict(list)
    for r in runs:
        by_night[r[2]].append(r)
    for night in sorted(by_night):
        k = np.array(session.execute(select(KnownTracklet.id, KnownTracklet.epoch, KnownTracklet.ra_epoch,
                                            KnownTracklet.dec_epoch, KnownTracklet.vra, KnownTracklet.vdec,
                                            KnownTracklet.mag)
                                     .where(KnownTracklet.source == "mpc_linked", KnownTracklet.night == night,
                                            KnownTracklet.epoch.is_not(None)).order_by(KnownTracklet.id)).all(),
                     dtype=float).reshape(-1, 7)
        if not len(k):
            continue
        speed = np.hypot(k[:, 4], k[:, 5])
        # per tracklet: best chip by exposures on chip (ignoring velocity), and whether any on-chip search covers its speed
        best = {}
        inrange = set()
        for definition_id, run_id, _, v0, v1 in by_night[night]:
            ex = session.execute(select(SearchDefinitionExposure.midpoint_mjd, SearchDefinitionExposure.ra_00,
                                        SearchDefinitionExposure.dec_00, SearchDefinitionExposure.ra_01,
                                        SearchDefinitionExposure.dec_01, SearchDefinitionExposure.ra_10,
                                        SearchDefinitionExposure.dec_10, SearchDefinitionExposure.ra_11,
                                        SearchDefinitionExposure.dec_11)
                                 .where(SearchDefinitionExposure.search_definition_id == definition_id)
                                 .order_by(SearchDefinitionExposure.midpoint_mjd)).all()
            ex = np.array([e for e in ex if None not in e], dtype=float).reshape(-1, 9)
            if not len(ex):
                continue
            ex = ex[np.argsort(ex[:, 0])]
            times = exposure_times_tai(ex[:, 0])
            if len(times) != len(ex):
                continue
            ra, dec = positions(k[:, 2] - k[:, 4] * k[:, 1], k[:, 3] - k[:, 5] * k[:, 1], k[:, 4], k[:, 5], times)
            on = inside_quadrilaterals(ra, dec, ex[:, [1, 3, 5, 7]], ex[:, [2, 4, 6, 8]]).sum(axis=1)
            need = max(1, int(np.ceil(0.5 * len(times))))
            for i in np.nonzero(on >= need)[0]:
                kid = int(k[i, 0])
                ok = v0 < speed[i] < v1
                if ok:
                    inrange.add(kid)
                # prefer an in-range chip, then most exposures
                key = (ok, int(on[i]))
                if kid not in best or key > best[kid][0]:
                    best[kid] = (key, run_id, len(times), v0, v1)
                lo, hi = best[kid][5:7] if len(best[kid]) > 5 else (v0, v1)
                best[kid] = best[kid][:5] + (min(lo, v0), max(hi, v1))
        if best:
            ids = list(best)
            rec = set()
            for c in range(0, len(ids), 500):
                q = select(CrossmatchCandidate.known_tracklet_id).where(
                    CrossmatchCandidate.known_tracklet_id.in_(ids[c:c + 500]),
                    CrossmatchCandidate.mean_sep_arcsec < RADIUS)
                if IN_BAND is not None:
                    q = q.where(CrossmatchCandidate.result_id.in_(IN_BAND))
                rec.update(session.scalars(q.distinct()).all())
            idx = {int(x): i for i, x in enumerate(k[:, 0])}
            for kid, b in best.items():
                i = idx[kid]
                out.write(f"{kid},{night},{speed[i]},{k[i, 6]},{b[1]},{b[0][1]},{b[2]},{b[3]},{b[4]},"
                          f"{int(kid in inrange)},{b[5]},{b[6]},{int(kid in rec)}\n")
        out.flush()
        print(night, len(best), flush=True)
