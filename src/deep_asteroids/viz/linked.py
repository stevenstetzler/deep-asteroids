"""
RA/Dec plots of linked MPC tracklets, for checking the link step.

For each ``mpc_linked`` row built from more than one ``trkid`` (``deep-asteroids
link``), one figure with two panels:

* **sky** (RA/Dec): every member's own observations (re-read from deep-db), its
  own fit as a solid line over the time it was observed and dashed across the
  rest of the night, and the merged fit as a thick grey line underneath. If the
  link is right the members' observations lie along one track, the members'
  dashed lines run together, and the merged line follows all of them.
* **agreement**: the separation of each member's fit from the merged fit at every
  exposure of the night, in arcsec. The link test is that members agree on
  average over those times to within the match radius, so these curves should
  stay near or below a few tenths of an arcsec; a member that wanders away is a
  bad merge. Each member's observed span is shaded.
"""
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from sqlalchemy import select, text

from deep_asteroids.crossmatch.geometry import exposure_times_tai
from deep_asteroids.database.tracklets import KnownTracklet, KnownTrackletMember
from deep_asteroids.ingest.fit import predict, sep_arcsec
from deep_asteroids.ingest.link import NIGHT_EXPOSURES, _night_points

MAX_LEGEND = 12  # members listed in the legend; a bigger group is summarized


@dataclass
class LinkedGroup:
    """One ``mpc_linked`` row and the ``mpc_observation`` rows it was built from."""
    linked: dict
    members: list  # dicts, ordered by trkid


@dataclass
class LinkedVizSummary:
    night: int
    n_groups: int = 0
    paths: list = field(default_factory=list)

    def format(self):
        return f"linked tracklets, night {self.night}: {self.n_groups} figure(s) written"


_FIT = ("epoch", "ra_epoch", "dec_epoch", "vra", "vdec", "t_min", "t_max", "n_points")


def load_groups(session, night, min_members=2, linked_ids=None):
    """The night's ``mpc_linked`` rows with at least ``min_members`` members (or just
    ``linked_ids``), largest first, with their members."""
    q = select(KnownTracklet).where(
        KnownTracklet.source == "mpc_linked", KnownTracklet.night == night, KnownTracklet.n_members >= min_members)
    if linked_ids:
        q = q.where(KnownTracklet.id.in_(list(linked_ids)))
    rows = session.scalars(q.order_by(KnownTracklet.n_members.desc(), KnownTracklet.id)).all()
    groups = []
    for k in rows:
        members = session.scalars(
            select(KnownTracklet).join(KnownTrackletMember, KnownTrackletMember.member_id == KnownTracklet.id)
            .where(KnownTrackletMember.linked_id == k.id).order_by(KnownTracklet.trkid)).all()
        info = lambda r: {c: getattr(r, c) for c in (*_FIT, "id", "night", "trkid", "trksub", "provid", "statuses",
                                                      "deep_db_mpc_tracklet_id", "n_members", "designations",
                                                      "n_designations", "flag")}
        groups.append(LinkedGroup(linked=info(k), members=[info(m) for m in members]))
    return groups


def _colors(n):
    import matplotlib.pyplot as plt

    cmap = plt.get_cmap("tab10" if n <= 10 else "tab20")
    return [cmap(i % cmap.N) for i in range(n)]


def render_linked_group(group, points, grid_tai, path):
    """Write the two-panel figure for ``group`` to ``path``. ``points`` is
    ``(tracklet_id, t_tai, ra, dec)`` arrays of the night's MPC observations (``None`` if there
    are none) and ``grid_tai`` the night's exposure times (TAI MJD)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    k, members = group.linked, group.members
    grid = np.asarray(grid_tai, dtype=float)
    t0 = grid.min()
    hours = lambda t: (np.asarray(t) - t0) * 24.0
    colors = _colors(len(members))
    markers = "osD^v<>PXh*"

    fig, (ax_sky, ax_sep) = plt.subplots(1, 2, figsize=(15, 6), dpi=100, gridspec_kw={"width_ratios": [1.3, 1]})
    ra_m, dec_m = predict(k["epoch"], k["ra_epoch"], k["dec_epoch"], k["vra"], k["vdec"], grid)
    ax_sky.plot(ra_m, dec_m, "-", color="0.6", lw=7, alpha=0.6, zorder=1, solid_capstyle="round",
                label=f"merged fit ({k['n_points']} obs)")

    for i, (m, c) in enumerate(zip(members, colors)):
        label = f"{m['trkid']} ({m['n_points']} obs{', ' + m['provid'] if m['provid'] else ''})"
        shown = label if i < MAX_LEGEND else None
        span = np.linspace(m["t_min"], m["t_max"], 20)
        ra_s, dec_s = predict(m["epoch"], m["ra_epoch"], m["dec_epoch"], m["vra"], m["vdec"], span)
        ra_f, dec_f = predict(m["epoch"], m["ra_epoch"], m["dec_epoch"], m["vra"], m["vdec"], grid)
        ax_sky.plot(ra_f, dec_f, "--", color=c, lw=1, alpha=0.8, zorder=2)
        ax_sky.plot(ra_s, dec_s, "-", color=c, lw=2, zorder=3)
        if points is not None:
            sel = points[0] == m["deep_db_mpc_tracklet_id"]
            ax_sky.scatter(points[2][sel], points[3][sel], s=28, color=c, marker=markers[i % len(markers)],
                           edgecolor="k", linewidth=0.4, zorder=4, label=shown)
        elif shown:
            ax_sky.plot([], [], color=c, label=shown)
        sep = sep_arcsec(ra_f, dec_f, ra_m, dec_m)
        ax_sep.axvspan(hours(m["t_min"]), hours(m["t_max"]), color=c, alpha=0.12, lw=0)
        ax_sep.plot(hours(grid), sep, "-o", color=c, ms=3, lw=1.2, label=None)

    if len(members) > MAX_LEGEND:
        ax_sky.plot([], [], " ", label=f"... and {len(members) - MAX_LEGEND} more")
    ax_sky.set_xlabel("RA (deg)")
    ax_sky.set_ylabel("Dec (deg)")
    ax_sky.ticklabel_format(useOffset=False)
    ax_sky.set_aspect(1.0 / np.cos(np.radians(np.mean(dec_m))), adjustable="datalim")
    ax_sky.legend(loc="best", fontsize=7, framealpha=0.85)
    ax_sky.grid(alpha=0.25)
    ax_sky.set_title("sky: observations, each member's fit (solid where observed, dashed across the night), "
                     "merged fit (grey)", fontsize=8)

    ax_sep.set_xlabel("hours since the night's first exposure")
    ax_sep.set_ylabel("separation of a member's fit from the merged fit (arcsec)")
    ax_sep.set_title("agreement over the night's exposures (shaded: each member's observed span)", fontsize=8)
    ax_sep.grid(alpha=0.25)
    ax_sep.set_ylim(bottom=0)

    desig = k["designations"] or "undesignated"
    flag = f", flag {k['flag']}" if k["flag"] else ""
    warn = "   CONFLICTING DESIGNATIONS" if (k["n_designations"] or 0) > 1 else ""
    fig.suptitle(f"night {k['night']}: linked tracklet {k['id']} = {k['n_members']} MPC tracklets, "
                 f"{k['n_points']} observations, statuses {k['statuses']}, {desig}{flag}{warn}", fontsize=10,
                 color="C3" if warn else "k")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return Path(path)


def contact_sheets(paths, out_dir, per_sheet=6, columns=2, width=1500):
    """Tile the figures into sheets (``contact_sheet_01.png`` ...), in the order given.
    Returns the sheet paths."""
    from PIL import Image

    sheets = []
    for s, start in enumerate(range(0, len(paths), per_sheet), start=1):
        chunk = paths[start:start + per_sheet]
        tile_w = width // columns
        tiles = []
        for p in chunk:
            im = Image.open(p).convert("RGB")
            tiles.append(im.resize((tile_w, int(im.height * tile_w / im.width))))
        rows = [tiles[i:i + columns] for i in range(0, len(tiles), columns)]
        height = sum(max(t.height for t in row) for row in rows)
        sheet = Image.new("RGB", (tile_w * columns, height), "white")
        y = 0
        for row in rows:
            for x, t in enumerate(row):
                sheet.paste(t, (x * tile_w, y))
            y += max(t.height for t in row)
        out = Path(out_dir) / f"contact_sheet_{s:02d}.png"
        sheet.save(out)
        sheets.append(out)
    return sheets


def visualize_linked(session, deep_engine, night, out_dir, min_members=2, linked_ids=None, limit=None,
                     sheets=True):
    """Write one figure per merged ``mpc_linked`` tracklet of ``night`` into ``out_dir`` (and, with
    ``sheets``, contact sheets of them). Returns a ``LinkedVizSummary``."""
    out_dir = Path(out_dir)
    summary = LinkedVizSummary(night=night)
    groups = load_groups(session, night, min_members=min_members, linked_ids=linked_ids)
    if limit:
        groups = groups[:limit]
    if not groups:
        return summary
    with deep_engine.connect() as conn:
        grid = exposure_times_tai([r[0] for r in conn.execute(text(NIGHT_EXPOSURES), {"night": night})])
    points = _night_points(deep_engine, [night])[night]
    for g in groups:
        k = g.linked
        path = out_dir / f"linked_{night}_{k['id']:05d}_{k['n_members']}members.png"
        summary.paths.append(render_linked_group(g, points, grid, path))
    summary.n_groups = len(groups)
    if sheets:
        summary.paths.extend(contact_sheets(summary.paths[:summary.n_groups], out_dir))
    return summary
