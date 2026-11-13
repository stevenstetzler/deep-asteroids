"""
Group results by what they match, and call each putative real or not.

**Groups** (disjoint, every result in exactly one), ported from salad's
``summarize_results.py``/``plot_residual_by_group.py``. A result is

* ``submitted`` if its closest linked MPC tracklet (``mpc_linked``) is within the submitted radius (2"),
* ``synthetic`` if its closest synthetic object is within the synthetic radius (2"),
  (when it is both, the closer one),
* else ``known`` if its closest mpchecker ephemeris is within the known radius (10"),
* else unmatched, ``unmatched (in velocity range)`` or ``unmatched (outside velocity range)`` according to
  whether its fitted speed ``hypot(vra, vdec)`` is inside its own search's ``(velocity_0, velocity_1)``.

**Putative real** is a cut on the result's own numbers, needing no cross-match, from salad's analysis (the
median residual of a result's gathered points against its own tracklet is bimodal: a few tenths of an arcsec for
real moving objects, arcsec-scale for chance alignments): the median residual below ``10**log10_residual``
(``-0.2``, 0.63"), more than ``min_gathered`` and fewer than ``max_gathered`` gathered points, and a speed strictly
inside its search's velocity range. **Putative new** is putative real *and unmatched*: a candidate no known object
explains. Counting objects uses the authoritative result of each de-duplication cluster
(``crossmatch/dedup.py``); a result never de-duplicated counts as its own.
"""
from dataclasses import dataclass, field

from find_asteroids.models import Result, Search
from sqlalchemy import func, select

from deep_asteroids.database.crossmatch import CrossmatchCandidate, ResultDedup
from deep_asteroids.database.search import SearchDefinition
from deep_asteroids.database.statistics import ResultStatistics

SUBMITTED = "submitted"
SYNTHETIC = "synthetic"
KNOWN = "known"
UNMATCHED_IN = "unmatched (in velocity range)"
UNMATCHED_OUT = "unmatched (outside velocity range)"
GROUPS = (SUBMITTED, SYNTHETIC, KNOWN, UNMATCHED_IN, UNMATCHED_OUT)
MATCH_GROUPS = (SUBMITTED, SYNTHETIC, KNOWN)
# the known_tracklet source each matching group is decided from
GROUP_SOURCE = {SUBMITTED: "mpc_linked", SYNTHETIC: "synthetic", KNOWN: "mpchecker"}


@dataclass
class Thresholds:
    submitted_radius_arcsec: float = 2.0
    synthetic_radius_arcsec: float = 2.0
    known_radius_arcsec: float = 10.0
    log10_residual: float = -0.2  # putative real: log10(median residual / arcsec) below this
    min_gathered: int = 10  # ... more than this many gathered points
    max_gathered: int = 150  # ... and fewer than this many

    def radius(self, group):
        return {SUBMITTED: self.submitted_radius_arcsec, SYNTHETIC: self.synthetic_radius_arcsec,
                KNOWN: self.known_radius_arcsec}[group]


@dataclass
class ResultRow:
    """A result with its statistics and closest match of each source, and (after ``classify``)
    its group and whether it is putative real."""
    result_id: int
    run_id: str
    result_num: int
    night: int | None
    authoritative: bool
    n_gathered: int
    median_residual_arcsec: float | None
    speed: float
    sigma_v: float | None
    median_mag: float | None
    velocity_0: float
    velocity_1: float
    best: dict = field(default_factory=dict)  # source -> closest mean separation (arcsec)
    group: str | None = None
    putative_real: bool | None = None
    failed: tuple = ()  # which cuts a not-real result fails: "residual", "n_gathered", "velocity"

    @property
    def in_velocity_range(self):
        return self.velocity_0 < self.speed < self.velocity_1

    @property
    def putative_new(self):
        return bool(self.putative_real) and self.group in (UNMATCHED_IN, UNMATCHED_OUT)


class MissingStatisticsError(ValueError):
    """Some selected results have no ``result_statistics`` row."""


def load_results(session, nights=None, run_ids=None):
    """Every selected result with its statistics and closest candidate per source (not yet classified).
    Raises ``MissingStatisticsError`` if any selected result lacks statistics."""
    q = (
        select(Result.id, Search.run_id, Result.result_num, SearchDefinition.night, ResultDedup.is_authoritative,
               ResultStatistics.n_gathered, ResultStatistics.median_residual_arcsec, ResultStatistics.speed,
               ResultStatistics.sigma_v, ResultStatistics.median_mag, Search.velocity_0, Search.velocity_1)
        .join(Search, Search.id == Result.search_id)
        .outerjoin(ResultStatistics, ResultStatistics.result_id == Result.id)
        .outerjoin(SearchDefinition, SearchDefinition.run_id == Search.run_id)
        .outerjoin(ResultDedup, ResultDedup.result_id == Result.id)
    )
    if nights:
        q = q.where(SearchDefinition.night.in_(list(nights)))
    if run_ids:
        q = q.where(Search.run_id.in_(list(run_ids)))
    rows = session.execute(q.order_by(Search.run_id, Result.result_num)).all()
    missing = [r for r in rows if r[5] is None]
    if missing:
        raise MissingStatisticsError(
            f"{len(missing)} selected result(s) have no statistics: run `deep-asteroids statistics` first")
    ids = [r[0] for r in rows]
    best = {}
    for chunk in range(0, len(ids), 500):
        for source, result_id, sep in session.execute(
                select(CrossmatchCandidate.source, CrossmatchCandidate.result_id,
                       func.min(CrossmatchCandidate.mean_sep_arcsec))
                .where(CrossmatchCandidate.result_id.in_(ids[chunk:chunk + 500]))
                .group_by(CrossmatchCandidate.source, CrossmatchCandidate.result_id)):
            best.setdefault(result_id, {})[source] = sep
    return [ResultRow(
        result_id=r[0], run_id=r[1], result_num=r[2], night=r[3], authoritative=True if r[4] is None else bool(r[4]),
        n_gathered=r[5], median_residual_arcsec=r[6], speed=r[7], sigma_v=r[8], median_mag=r[9],
        velocity_0=r[10], velocity_1=r[11], best=best.get(r[0], {})) for r in rows]


def classify(rows, thresholds=None):
    """Set ``group``, ``putative_real`` and ``failed`` on each row (default ``Thresholds()``). Returns ``rows``."""
    thresholds = thresholds or Thresholds()
    for r in rows:
        matches = [(r.best[GROUP_SOURCE[g]], g) for g in (SUBMITTED, SYNTHETIC)
                   if GROUP_SOURCE[g] in r.best and r.best[GROUP_SOURCE[g]] < thresholds.radius(g)]
        if matches:
            r.group = min(matches)[1]
        elif "mpchecker" in r.best and r.best["mpchecker"] < thresholds.known_radius_arcsec:
            r.group = KNOWN
        else:
            r.group = UNMATCHED_IN if r.in_velocity_range else UNMATCHED_OUT

        failed = []
        if r.median_residual_arcsec is None or not (
                _log10(r.median_residual_arcsec) < thresholds.log10_residual):
            failed.append("residual")
        if not thresholds.min_gathered < r.n_gathered < thresholds.max_gathered:
            failed.append("n_gathered")
        if not r.in_velocity_range:
            failed.append("velocity")
        r.failed = tuple(failed)
        r.putative_real = not failed
    return rows


def _log10(x):
    import math

    return math.log10(x) if x > 0 else float("-inf")


@dataclass
class Funnel:
    """Counts of the authoritative results (distinct objects), by group and by putative real."""
    n_results: int
    n_objects: int
    by_group: dict  # group -> (n, n_putative_real)
    n_putative_real: int
    n_putative_new: int

    def format(self):
        lines = [f"  {'':<38} {'objects':>8} {'putative real':>14}  {'fraction':>8}"]
        lines.append(f"  {'all results / distinct objects':<38} {self.n_results:>8,} / {self.n_objects:<6,}")
        for g in GROUPS:
            n, real = self.by_group[g]
            lines.append(f"  {g:<38} {n:>8,} {real:>14,}  {real / n if n else float('nan'):>8.1%}")
        lines.append(f"  {'total':<38} {self.n_objects:>8,} {self.n_putative_real:>14,}  "
                     f"{self.n_putative_real / self.n_objects if self.n_objects else float('nan'):>8.1%}")
        lines.append(f"  putative new (putative real, matching nothing): {self.n_putative_new:,}")
        return "\n".join(lines)


def retained(rows):
    """For each matching group, the fraction of its authoritative results that pass the putative-real cut:
    what the cut keeps of objects we know are real (the closest thing to a recall)."""
    out = {}
    for g in MATCH_GROUPS:
        members = [r for r in rows if r.authoritative and r.group == g]
        out[g] = (sum(bool(r.putative_real) for r in members), len(members))
    return out


def why_rejected(rows):
    """``{cut: n}``: how many authoritative results fail each cut of the putative-real definition
    (a result can fail several)."""
    out = {}
    for r in rows:
        if r.authoritative and not r.putative_real:
            for cut in r.failed:
                out[cut] = out.get(cut, 0) + 1
    return out


def funnel(rows):
    """``Funnel`` of the authoritative rows of an already-classified ``rows``."""
    objects = [r for r in rows if r.authoritative]
    by_group = {g: (sum(r.group == g for r in objects), sum(r.group == g and r.putative_real for r in objects))
                for g in GROUPS}
    return Funnel(
        n_results=len(rows), n_objects=len(objects), by_group=by_group,
        n_putative_real=sum(bool(r.putative_real) for r in objects),
        n_putative_new=sum(r.putative_new for r in objects))
