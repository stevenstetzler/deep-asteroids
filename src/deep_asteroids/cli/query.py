import click

from deep_asteroids.cli import main

DEFAULT_SEARCH_FIELDS = (
    "night", "field", "detector_number", "run_id", "band", "n_exposures", "t_start_mjd", "t_end_mjd",
)
DEFAULT_IMAGE_FIELDS = ("night", "field", "detector_number", "expnum", "product_type", "uri")
DEFAULT_TRACKLET_FIELDS = (
    "source", "night", "component", "orbit_id", "trkid", "provid",
    "epoch", "ra_epoch", "dec_epoch", "vra", "vdec", "n_points", "max_residual_arcsec", "flag",
)


@main.group()
@click.pass_context
def query(ctx):
    """Query the deep-asteroids database."""


@query.command()
@click.argument("fields", nargs=-1, type=str)
@click.option("--night", "nights", multiple=True, type=int)
@click.option("--field", "fields_", multiple=True, type=str, help="deep-db field name (repeatable)")
@click.option("--detector-number", "detector_numbers", multiple=True, type=int)
@click.option("--band", "bands", multiple=True, type=str,
              help="Only rows with this band (repeatable); '' or 'none' selects the undefined-band "
                   "(band=NULL) rows")
@click.option("--header/--no-header", default=False, help="Print the field names first")
@click.option("--output", type=click.Path(dir_okay=False), help="Write here instead of stdout")
@click.pass_context
def searches(ctx, fields, nights, fields_, detector_numbers, bands, header, output):
    """Print search_definition rows, '|'-separated (deep-db's query style).

    FIELDS are search_definition column names (default: a summary set)."""
    from sqlalchemy import or_, select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.database.search import SearchDefinition

    fields = fields or DEFAULT_SEARCH_FIELDS
    columns = SearchDefinition.__table__.columns
    unknown = [f for f in fields if f not in columns]
    if unknown:
        raise click.BadParameter(f"unknown field(s) {unknown}; choose from {list(columns.keys())}",
                                 param_hint="FIELDS")

    q = select(*(columns[f] for f in fields))
    for column, values in (("night", nights), ("field", fields_), ("detector_number", detector_numbers)):
        if values:
            q = q.where(columns[column].in_(values))
    if bands:
        null_sentinels = {"", "none"}
        named = [b for b in bands if b not in null_sentinels]
        conditions = ([columns["band"].in_(named)] if named else []) + (
            [columns["band"].is_(None)] if set(bands) & null_sentinels else [])
        q = q.where(or_(*conditions))
    q = q.order_by(columns["id"])

    with click.open_file(output or "-", "w") as out, Session(get_db()) as session:
        if header:
            out.write("|".join(fields) + "\n")
        for row in session.execute(q):
            out.write("|".join("" if v is None else str(v) for v in row) + "\n")


@query.command()
@click.argument("fields", nargs=-1, type=str)
@click.option("--night", "nights", multiple=True, type=int)
@click.option("--field", "fields_", multiple=True, type=str, help="deep-db field name (repeatable)")
@click.option("--detector-number", "detector_numbers", multiple=True, type=int)
@click.option("--product-type", "product_types", multiple=True, type=str)
@click.option("--header/--no-header", default=False, help="Print the field names first")
@click.option("--output", type=click.Path(dir_okay=False), help="Write here instead of stdout")
@click.pass_context
def images(ctx, fields, nights, fields_, detector_numbers, product_types, header, output):
    """Print image rows, '|'-separated (deep-db's query style).

    FIELDS are image column names (default: a summary set)."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.database.images import Image

    fields = fields or DEFAULT_IMAGE_FIELDS
    columns = Image.__table__.columns
    unknown = [f for f in fields if f not in columns]
    if unknown:
        raise click.BadParameter(f"unknown field(s) {unknown}; choose from {list(columns.keys())}",
                                 param_hint="FIELDS")

    q = select(*(columns[f] for f in fields))
    for column, values in (("night", nights), ("field", fields_), ("detector_number", detector_numbers),
                           ("product_type", product_types)):
        if values:
            q = q.where(columns[column].in_(values))
    q = q.order_by(columns["id"])

    with click.open_file(output or "-", "w") as out, Session(get_db()) as session:
        if header:
            out.write("|".join(fields) + "\n")
        for row in session.execute(q):
            out.write("|".join("" if v is None else str(v) for v in row) + "\n")


@query.command()
@click.argument("fields", nargs=-1, type=str)
@click.option("--source", "sources", multiple=True,
              type=click.Choice(["synthetic", "mpc_observation", "mpc_linked", "mpchecker"]))
@click.option("--night", "nights", multiple=True, type=int)
@click.option("--provid", "provids", multiple=True, type=str)
@click.option("--trkid", "trkids", multiple=True, type=str)
@click.option("--orbit-id", "orbit_ids", multiple=True, type=int)
@click.option("--component", "components", multiple=True,
              type=click.Choice(["primary", "companion", "barycenter", "photocenter"]))
@click.option("--flag", "flags", multiple=True, type=str,
              help="Only rows with this flag (repeatable); 'none' selects unflagged rows")
@click.option("--header/--no-header", default=False, help="Print the field names first")
@click.option("--output", type=click.Path(dir_okay=False), help="Write here instead of stdout")
@click.pass_context
def tracklets(ctx, fields, sources, nights, provids, trkids, orbit_ids, components, flags, header, output):
    """Print known_tracklet rows, '|'-separated (deep-db's query style).

    FIELDS are known_tracklet column names (default: a summary set)."""
    from sqlalchemy import or_, select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.database.tracklets import KnownTracklet

    fields = fields or DEFAULT_TRACKLET_FIELDS
    columns = KnownTracklet.__table__.columns
    unknown = [f for f in fields if f not in columns]
    if unknown:
        raise click.BadParameter(f"unknown field(s) {unknown}; choose from {list(columns.keys())}",
                                 param_hint="FIELDS")

    q = select(*(columns[f] for f in fields))
    for column, values in (("source", sources), ("night", nights), ("provid", provids), ("trkid", trkids),
                           ("orbit_id", orbit_ids), ("component", components)):
        if values:
            q = q.where(columns[column].in_(values))
    if flags:
        named = [f for f in flags if f != "none"]
        conditions = ([columns["flag"].in_(named)] if named else []) + (
            [columns["flag"].is_(None)] if "none" in flags else [])
        q = q.where(or_(*conditions))
    q = q.order_by(columns["id"])

    with click.open_file(output or "-", "w") as out, Session(get_db()) as session:
        if header:
            out.write("|".join(fields) + "\n")
        for row in session.execute(q):
            out.write("|".join("" if v is None else str(v) for v in row) + "\n")


CROSSMATCH_FIELDS = (
    "run_id", "result_num", "authoritative", "source", "mean_sep_arcsec", "min_sep_arcsec", "max_sep_arcsec",
    "velocity_diff", "known_tracklet_id", "trkid", "provid", "orbit_id", "component", "members",
)


@query.command()
@click.option("--source", "sources", multiple=True,
              type=click.Choice(["synthetic", "mpc_observation", "mpc_linked", "mpchecker"]))
@click.option("--night", "nights", multiple=True, type=int)
@click.option("--run-id", "run_ids", multiple=True, type=str)
@click.option("--max-mean-sep-arcsec", type=float, default=None, help="Only candidates below this mean separation")
@click.option("--all", "all_candidates", is_flag=True,
              help="Every stored candidate; default: only the closest of each source for each result")
@click.option("--authoritative-only", is_flag=True,
              help="Only results that are authoritative in their de-duplication cluster (or were never de-duplicated)")
@click.option("--header/--no-header", default=False, help="Print the field names first")
@click.option("--output", type=click.Path(dir_okay=False), help="Write here instead of stdout")
@click.pass_context
def crossmatches(ctx, sources, nights, run_ids, max_mean_sep_arcsec, all_candidates, authoritative_only, header,
                 output):
    """Print cross-match candidates, '|'-separated: the closest of each source
    for each result (or every candidate, with --all).

    Fields: run_id result_num authoritative source mean_sep_arcsec min_sep_arcsec
    max_sep_arcsec velocity_diff known_tracklet_id trkid provid orbit_id component members.
    `authoritative` is 1/0, or empty if the result was never de-duplicated. For
    an `mpc_linked` match `members` lists the trkids it was linked from (and
    `trkid` is empty); `provid` is empty if its members carry no designation or
    more than one."""
    from sqlalchemy import func, or_, select
    from sqlalchemy.orm import Session

    from deep_asteroids.database import get_db
    from deep_asteroids.database.crossmatch import CrossmatchCandidate as C
    from deep_asteroids.database.crossmatch import ResultDedup
    from deep_asteroids.database.tracklets import KnownTracklet as K
    from deep_asteroids.database.tracklets import KnownTrackletMember
    from find_asteroids.models import Result, Search

    rank = func.row_number().over(
        partition_by=(C.result_id, C.source), order_by=(C.mean_sep_arcsec, C.id)).label("rank")
    q = (
        select(Search.run_id, Result.result_num, ResultDedup.is_authoritative, C.source, C.mean_sep_arcsec,
               C.min_sep_arcsec, C.max_sep_arcsec, C.velocity_diff, K.id, K.trkid, K.provid, K.orbit_id,
               K.component, rank)
        .join(Result, Result.id == C.result_id).join(Search, Search.id == Result.search_id)
        .join(K, K.id == C.known_tracklet_id).outerjoin(ResultDedup, ResultDedup.result_id == C.result_id)
    )
    if sources:
        q = q.where(C.source.in_(sources))
    if nights:
        q = q.where(C.night.in_(nights))
    if run_ids:
        q = q.where(Search.run_id.in_(run_ids))
    if max_mean_sep_arcsec is not None:
        q = q.where(C.mean_sep_arcsec < max_mean_sep_arcsec)
    if authoritative_only:
        q = q.where(or_(ResultDedup.is_authoritative.is_(None), ResultDedup.is_authoritative.is_(True)))
    sub = q.subquery()
    outer = select(*(c for c in list(sub.c)[:-1]))
    if not all_candidates:
        outer = outer.where(sub.c.rank == 1)
    outer = outer.order_by(sub.c.run_id, sub.c.result_num, sub.c.source, sub.c.mean_sep_arcsec)

    def fmt(v):
        return "" if v is None else (int(v) if isinstance(v, bool) else v)

    with click.open_file(output or "-", "w") as out, Session(get_db()) as session:
        if header:
            out.write("|".join(CROSSMATCH_FIELDS) + "\n")
        rows = session.execute(outer).all()
        linked = [r[8] for r in rows if r[3] == "mpc_linked"]
        members = {}
        if linked:
            for linked_id, trkid in session.execute(
                select(KnownTrackletMember.linked_id, K.trkid).join(K, K.id == KnownTrackletMember.member_id)
                .where(KnownTrackletMember.linked_id.in_(set(linked))).order_by(K.trkid)):
                members.setdefault(linked_id, []).append(trkid)
        for row in rows:
            out.write("|".join(str(fmt(v)) for v in (*row, ",".join(members.get(row[8], [])))) + "\n")


RESULT_FIELDS = (
    "run_id", "result_num", "authoritative", "group", "putative_real", "failed_cuts", "n_gathered",
    "median_residual_arcsec", "speed", "sigma_v", "median_mag", "sep_submitted", "sep_synthetic", "sep_known",
)


@query.command()
@click.option("--night", "nights", multiple=True, type=int)
@click.option("--run-id", "run_ids", multiple=True, type=str)
@click.option("--group", "groups", multiple=True,
              type=click.Choice(["submitted", "synthetic", "known", "unmatched (in velocity range)",
                                 "unmatched (outside velocity range)"]))
@click.option("--putative-real/--not-putative-real", "putative_real", default=None,
              help="Only results that pass (or fail) the putative-real cut")
@click.option("--putative-new", is_flag=True, help="Only putative real results that match nothing")
@click.option("--authoritative-only", is_flag=True,
              help="Only the authoritative result of each de-duplication cluster")
@click.option("--sigma-v-threshold", type=float, default=0.00025, show_default=True)
@click.option("--limit", type=int, default=None, help="Only the first N results")
@click.option("--header/--no-header", default=False, help="Print the field names first")
@click.option("--output", type=click.Path(dir_okay=False), help="Write here instead of stdout")
@click.pass_context
def results(ctx, nights, run_ids, groups, putative_real, putative_new, authoritative_only, sigma_v_threshold,
            limit, header, output):
    """Print results with their statistics, group and putative-real call, '|'-separated.

    Fields: run_id result_num authoritative group putative_real failed_cuts n_gathered
    median_residual_arcsec speed sigma_v median_mag sep_submitted sep_synthetic sep_known (the closest
    match of each kind, in arcsec). Needs `statistics` and `crossmatch`."""
    from sqlalchemy.orm import Session

    from deep_asteroids.cli.plot import classified, make_thresholds
    from deep_asteroids.database import create_db

    thresholds = make_thresholds(2.0, 2.0, 10.0, sigma_v_threshold)
    with Session(create_db()) as session:
        rows = classified(session, nights, run_ids, thresholds)
    if groups:
        rows = [r for r in rows if r.group in groups]
    if putative_real is not None:
        rows = [r for r in rows if bool(r.putative_real) == putative_real]
    if putative_new:
        rows = [r for r in rows if r.putative_new]
    if authoritative_only:
        rows = [r for r in rows if r.authoritative]
    if limit is not None:
        rows = rows[:limit]

    def fmt(v):
        return "" if v is None else int(v) if isinstance(v, bool) else v

    with click.open_file(output or "-", "w") as out:
        if header:
            out.write("|".join(RESULT_FIELDS) + "\n")
        for r in rows:
            values = (r.run_id, r.result_num, r.authoritative, r.group, r.putative_real, ",".join(r.failed),
                      r.n_gathered, r.median_residual_arcsec, r.speed, r.sigma_v, r.median_mag,
                      r.best.get("mpc_linked"), r.best.get("synthetic"), r.best.get("mpchecker"))
            out.write("|".join(str(fmt(v)) for v in values) + "\n")
