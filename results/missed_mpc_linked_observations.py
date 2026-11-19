"""One row per missed in-range mpc_linked tracklet: where it is, who it is, and its observations (obsids).

Reads the missed tracklets (in_range_any == 1 and recovered == 0) from mpc_linked_velocity_check.py's CSV, takes
each merged tracklet's observations from its member trkid tracklets, and matches them into deep-db on
(trkid, trksub) -- deep-db's own ids change between ingests, trkid/trksub do not.

Usage:

    python results/missed_mpc_linked_observations.py mpc_velocity.csv /path/to/deep_database.sqlite \
        /path/to/deep_asteroids_full_survey.sqlite results/missed_mpc_linked_observations.csv

Both databases are opened read-only. "ccd" lists the detector numbers the observations landed on, which can
differ from the chip in search_run_id (a tracklet counts as findable when its track crosses the searched chip).
"""
import csv, sqlite3, sys
csv_path, deep_path, ast_path, out = sys.argv[1:5]
ids = [(int(r["known_tracklet_id"]), r["night"], r["best_run_id"]) for r in csv.DictReader(open(csv_path))
       if r["in_range_any"] == "1" and r["recovered"] == "0"]
ast = sqlite3.connect(f"file:{ast_path}?mode=ro", uri=True)
deep = sqlite3.connect(f"file:{deep_path}?mode=ro", uri=True)
tracklet_id = {(t, s): i for i, t, s in deep.execute("select id, trkid, trksub from mpc_tracklet")}  # stable key across deep-db ingests
w = csv.writer(open(out, "w", newline=""))
w.writerow(["linked_id", "night", "field", "ccd", "search_run_id", "provid", "permid", "name", "n_obs", "obsids"])
for kid, night, run in ids:
    members = [tracklet_id.get(r) for r in ast.execute("select m.trkid, m.trksub from known_tracklet_member b join known_tracklet m on m.id=b.member_id where b.linked_id=?", (kid,))]
    if not members:  # singleton: its own trkid
        members = [tracklet_id.get(ast.execute("select trkid, trksub from known_tracklet where id=?", (kid,)).fetchone())]
    members = [m for m in members if m is not None]
    obs = deep.execute(f"""select o.obsid, f.name, dt.number, d.unpacked_provisional_designation, d.permanent_designation, d.name
        from mpc_observation o join exposure e on e.id=o.exposure_id join field f on f.id=e.field_id
        left join detector_exposure de on de.id=o.detector_exposure_id left join detector dt on dt.id=de.detector_id
        left join mpc_designation d on d.id=o.designation_id
        where o.mpc_tracklet_id in ({','.join('?'*len(members))}) order by o.obstime""", members).fetchall()
    uniq = lambda i: ";".join(sorted({str(r[i]) for r in obs if r[i] not in (None, "None")}))
    w.writerow([kid, night, uniq(1), uniq(2), run, uniq(3), uniq(4), uniq(5), len(obs), ";".join(r[0] for r in obs)])
print(len(ids), "tracklets")
