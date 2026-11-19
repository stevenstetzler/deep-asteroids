"""Search parity between salad's legacy searches and the band='VR' searches (#28).

For every legacy search (run_id without a -VR suffix), the exact exposures salad searched -- its
salad:image_ids=[...] catalog, mapped through salad's image table to (expnum, detector) -- are
compared with the exposures the VR search can search now: its band='VR' search definition's exposures
that have a differenceExp image. Also compares the image file each side used. Writes
parity_runs.csv (one row per legacy run) and parity_lost.csv (legacy-searched exposures the VR
search cannot search; header only when there are none) into OUT_DIR.

Needs salad's Postgres running (python -m salad.db start; PGHOST/PGPORT default localhost:55432).

    python results/legacy_vs_vr_parity.py deep_asteroids_full_survey.sqlite deep_database.sqlite OUT_DIR
"""
import os
import sys
import sqlite3, csv, re, collections, psycopg2

AST_DB, DEEP_DB, OUT_DIR = sys.argv[1:4]
ast=sqlite3.connect(f"file:{AST_DB}?mode=ro",uri=True)
deep=sqlite3.connect(f"file:{DEEP_DB}?mode=ro",uri=True)
detnum={nm:n for n,nm in deep.execute("select number,name from detector")}
q=lambda s,*a: ast.execute(s,a).fetchall()
res_sid=set(r[0] for r in q("select distinct search_id from result"))
leg={r:cat for i,r,cat in q("select id,run_id,catalog from search") if not r.endswith("-VR") and i in res_sid and cat.startswith("salad:image_ids")}
ids_by_run={r:[int(x) for x in re.findall(r"\d+",c.split("=",1)[1])] for r,c in leg.items()}
allids=sorted({i for v in ids_by_run.values() for i in v}); print("salad image ids used by legacy searches:",len(allids),flush=True)
pg=psycopg2.connect(host=os.environ.get("PGHOST","localhost"),port=int(os.environ.get("PGPORT",55432)),dbname="salad",user=os.environ.get("PGUSER") or os.environ["USER"]); cur=pg.cursor()
info={}  # id -> (expnum, detnum, collection_run, status, n_det)
pat=re.compile(r"/(\d+)/differenceExp_[^/]*_(\d+)_([NS]\d+)_DEEP_")
for k in range(0,len(allids),20000):
    cur.execute("select id,uri,status,n_detections from image where id = any(%s)",(allids[k:k+20000],))
    for i,uri,st,nd in cur.fetchall():
        m=pat.search(uri); 
        if not m: info[i]=(None,None,uri,st,nd); continue
        col=re.search(r"/(science#step\d+|diff_drp/[^/]+/[^/]+)/([0-9T]+Z)/",uri)
        info[i]=(int(m.group(2)),detnum.get(m.group(3)),(col.group(1)+"/"+col.group(2)) if col else uri,st,nd)
    print("fetched",min(k+20000,len(allids)),flush=True)
print("unparsed uris:",sum(1 for v in info.values() if v[0] is None),"; missing ids:",len(allids)-len(info),flush=True)
imgrow={d:(e,u) for d,e,u in q("select deep_db_detector_exposure_id,expnum,uri from image where product_type='differenceExp'")}
imgset=set(imgrow)
vdef={r[:-3]:i for i,r in q("select id,run_id from search_definition where band='VR'")}
ldef={r:i for i,r in q("select id,run_id from search_definition where band is null")}
def exps(sd): return {d:e for d,e in ast.execute("select deep_db_detector_exposure_id,expnum from search_definition_exposure where search_definition_id=?",(sd,))}
meta={r:(n,f,d) for r,n,f,d in q("select run_id,night,field,detector_number from search_definition where band is null")}
out=csv.writer(open(os.path.join(OUT_DIR,"parity_lost.csv"),"w")); out.writerow(["run_id","night","field","ccd","expnum","salad_image_id","salad_status","salad_n_detections","reason"])
runs=csv.writer(open(os.path.join(OUT_DIR,"parity_runs.csv"),"w")); runs.writerow(["run_id","salad_searched","vr_def","vr_searchable","legacy_not_searchable_now","searchable_not_in_legacy","same_file","different_collection","class"])
tot=collections.Counter(); samefile=diffcol=0
for run,ids in sorted(ids_by_run.items()):
    S={}
    for i in ids:
        e,d,col,st,nd=info.get(i,(None,None,None,None,None)); 
        if e is not None: S[e]=(i,col,st,nd)
    if run not in vdef: runs.writerow([run,len(S),None,None,None,None,None,None,"no VR definition"]); tot["no VR definition"]+=1; continue
    V=exps(vdef[run]); expnum_to_det={e:d for d,e in V.items()}
    Vimg={e:d for d,e in V.items() if d in imgset}
    lost=[e for e in S if e not in Vimg]; gained=[e for e in Vimg if e not in S]
    sf=dc=0
    for e in S.keys() & Vimg.keys():
        ours=imgrow[Vimg[e]][1]; col_ours=re.search(r"/(science#step\d+|diff_drp/[^/]+/[^/]+)/([0-9T]+Z)/",ours); 
        c2=(col_ours.group(1)+"/"+col_ours.group(2)) if col_ours else ours
        if c2==S[e][1]: sf+=1
        else: dc+=1
    samefile+=sf; diffcol+=dc
    cls="parity" if not lost and not gained else ("legacy searched exposures VR cannot" if lost else "VR can search more")
    if lost and gained: cls="both"
    tot[cls]+=1
    runs.writerow([run,len(S),len(V),len(Vimg),len(lost),len(gained),sf,dc,cls])
    n,f,dn=meta.get(run,(None,None,None))
    for e in lost:
        reason="not in the VR search definition" if e not in V.values() else "no differenceExp image"
        out.writerow([run,n,f,dn,e,S[e][0],S[e][2],S[e][3],reason])
print(dict(tot),flush=True); print("exposures searched by both: same image collection",samefile,"different collection",diffcol,flush=True); print("DONE",flush=True)
