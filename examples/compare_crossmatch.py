"""Compare our cross-match of a search run with salad's own, result by result.

usage: compare_crossmatch.py NEW_RUN_ID OLD_RUN_ID [--results-db URL] [--max-mean-sep-arcsec 30]

NEW_RUN_ID is a run in ``$DEEP_ASTEROIDS_DB`` that has been cross-matched (``deep-asteroids
crossmatch``); OLD_RUN_ID is salad's run of the same search in ``--results-db`` (default
``$RESULTS_DB``, salad's Postgres), whose ``crossmatch`` table keeps the single closest candidate
of each result. Results are paired by ``result_num``, which the two runs share when their Hough
searches agree (``compare_search.py`` checks that).

Salad's candidates are ``super_tracklet`` (its merged MPC observations), ``mpchecker`` and ``fake``
(synthetic); ours are the ``known_tracklet`` sources ``mpc_observation``, ``mpchecker`` and
``synthetic``. Salad crossmatched its tracklets at times 69 s off (its image table's times, see the
README), and kept one candidate per result, so exact agreement of separations is not expected; what
should agree is which results match which kind of object.
"""
import argparse
import os

from sqlalchemy import create_engine, text

SALAD_TO_OURS = {"super_tracklet": "mpc_observation", "mpchecker": "mpchecker", "fake": "synthetic"}

parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
parser.add_argument("new_run_id")
parser.add_argument("old_run_id")
parser.add_argument("--results-db", default=os.environ.get("RESULTS_DB"))
parser.add_argument("--max-mean-sep-arcsec", type=float, default=30.0)
args = parser.parse_args()
if not args.results_db:
    parser.error("set $RESULTS_DB or give --results-db")

salad = {}
with create_engine(args.results_db).connect() as conn:
    for num, source, obj, sep in conn.execute(text("""
        SELECT r.result_num, cm.source, cm."object", cm.mean_sep_arcsec
        FROM crossmatch cm JOIN result r ON r.id = cm.result_id JOIN search s ON s.id = r.search_id
        WHERE s.run_id = :run AND cm.mean_sep_arcsec < :max"""), {"run": args.old_run_id, "max": args.max_mean_sep_arcsec}):
        salad[num] = (SALAD_TO_OURS.get(source, source), obj, sep)

ours = {}  # result_num -> {source: (identity, mean_sep)}
with create_engine(os.environ["DEEP_ASTEROIDS_DB"]).connect() as conn:
    for num, source, sep, trkid, provid, orbit, comp in conn.execute(text("""
        SELECT r.result_num, c.source, c.mean_sep_arcsec, k.trkid, k.provid, k.orbit_id, k.component
        FROM crossmatch c JOIN result r ON r.id = c.result_id JOIN search s ON s.id = r.search_id
        JOIN known_tracklet k ON k.id = c.known_tracklet_id
        WHERE s.run_id = :run AND c.mean_sep_arcsec < :max ORDER BY c.mean_sep_arcsec"""),
            {"run": args.new_run_id, "max": args.max_mean_sep_arcsec}):
        identity = trkid or provid or f"orbit {orbit} {comp}"
        ours.setdefault(num, {}).setdefault(source, (identity, sep))

print(f"results with a candidate under {args.max_mean_sep_arcsec:g}\": salad {len(salad)}, ours {len(ours)}")
print(f"{'result':>6}  {'salad: kind / object / sep':<42}  ours: closest of each source")
agree = 0
for num in sorted(set(salad) | set(ours)):
    s = salad.get(num)
    left = f"{s[0]} / {s[1]} / {s[2]:.3f}\"" if s else "-"
    right = "; ".join(f"{src} {ident} {sep:.3f}\"" for src, (ident, sep) in sorted(ours.get(num, {}).items())) or "-"
    ok = s is not None and s[0] in ours.get(num, {})
    agree += ok
    print(f"{num:>6}  {left:<42}  {right}   {'ok' if ok else ('salad only' if s else 'ours only')}")
print(f"\nsalad's closest kind is among ours for {agree} of {len(salad)} results; "
      f"results only we match: {len(set(ours) - set(salad))}")
