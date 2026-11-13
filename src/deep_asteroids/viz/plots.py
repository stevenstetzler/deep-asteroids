"""
Population plots of a night's results: residuals by group, magnitudes by group, and completeness.

The groups and the putative-real cut are those of ``crossmatch/classify.py``. Residual and
magnitude plots count distinct objects (the authoritative result of each de-duplication
cluster) unless told otherwise.
"""
from pathlib import Path

import numpy as np

from deep_asteroids.crossmatch.classify import (
    GROUPS,
    KNOWN,
    SUBMITTED,
    SYNTHETIC,
    UNMATCHED_IN,
    UNMATCHED_OUT,
    Thresholds,
)
from deep_asteroids.crossmatch.completeness import bin_completeness

GROUP_COLORS = {
    SUBMITTED: "tab:green", SYNTHETIC: "tab:purple", KNOWN: "tab:blue",
    UNMATCHED_IN: "tab:orange", UNMATCHED_OUT: "tab:red",
}


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _save(fig, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    _plt().close(fig)
    return Path(path)


def _select(rows, authoritative_only):
    return [r for r in rows if r.authoritative] if authoritative_only else list(rows)


def _log_hist(rows, path, authoritative_only, value_of, xlabel, title, log10_range, cut=None):
    """Shared implementation of ``plot_residuals``/``plot_sigma_v``: overlaid histograms of
    ``log10(value_of(r))`` per group, optionally with a vertical line at ``cut`` (``(x, label)``)."""
    plt = _plt()
    rows = _select(rows, authoritative_only)
    fig, ax = plt.subplots(figsize=(9, 6))
    values = {g: [np.log10(value_of(r)) for r in rows if r.group == g and value_of(r)] for g in GROUPS}
    edges = np.linspace(*log10_range, 61)
    for g in GROUPS:
        v = [x for x in values[g] if log10_range[0] <= x <= log10_range[1]]
        if v:
            ax.hist(v, bins=edges, color=GROUP_COLORS[g], alpha=0.55, label=f"{g} (n={len(values[g]):,})")
    if cut is not None:
        x, label = cut
        ax.axvline(x, color="k", ls="--", lw=1, label=label)
    ax.set_xlabel(xlabel)
    ax.set_ylabel("results" if not authoritative_only else "distinct objects")
    ax.set_title(title)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)
    return _save(fig, path)


def plot_residuals(rows, path, authoritative_only=True, log10_range=(-2.0, 1.0)):
    """Overlaid histograms of ``log10(median residual)`` per group. A diagnostic, ported from salad's
    ``plot_residual_by_group.py`` -- it is *not* the putative-real cut (``sigma_v`` is; see
    ``plot_sigma_v``), though the two populations happen to separate at a similar point on this axis too."""
    return _log_hist(rows, path, authoritative_only, lambda r: r.median_residual_arcsec,
                     "log10(median residual of gathered points / arcsec)",
                     "residual of a result's points against its own tracklet, by what it matches", log10_range)


def plot_sigma_v(rows, path, thresholds=None, authoritative_only=True, log10_range=(-6.0, -1.0)):
    """Overlaid histograms of ``log10(sigma_v)`` per group, with the putative-real cut marked. ``sigma_v =
    hypot(sigma_vra, sigma_vdec)``, the fitted tracklet's velocity uncertainty -- salad's analysis
    (``docs/ANALYZE_RESULT_STATS.md``) found this the cleanest separator of real moving objects from chance
    alignments, after median residual and ``n_gathered`` both failed to separate them."""
    thresholds = thresholds or Thresholds()
    cut = (np.log10(thresholds.sigma_v_threshold), f"putative-real cut: sigma_v < {thresholds.sigma_v_threshold:g}")
    return _log_hist(rows, path, authoritative_only, lambda r: r.sigma_v, "log10(sigma_v / (deg/day))",
                     "tracklet fit velocity uncertainty, by what it matches", log10_range, cut)


NOT_PUTATIVE_REAL = "unmatched, not putative real"


def plot_magnitudes(rows, path, thresholds=None, authoritative_only=True, bin_width=0.5):
    """Three panels of each result's median magnitude, by group (the unmatched in-range results are split
    into putative real -- the candidates no known object explains -- and not): step histograms of raw
    counts (log scale), a linear-scale stacked total excluding ``unmatched, not putative real`` (which
    dominates by count and would otherwise swamp the panel), and each group's own fraction."""
    plt = _plt()
    rows = _select(rows, authoritative_only)
    series = {}
    for g in (SUBMITTED, SYNTHETIC, KNOWN):
        series[g] = (GROUP_COLORS[g], [r.median_mag for r in rows if r.group == g and r.median_mag is not None])
    series["putative new (unmatched, real)"] = ("tab:orange", [
        r.median_mag for r in rows if r.putative_new and r.median_mag is not None])
    series[NOT_PUTATIVE_REAL] = ("tab:red", [
        r.median_mag for r in rows if r.group in (UNMATCHED_IN, UNMATCHED_OUT) and not r.putative_real
        and r.median_mag is not None])
    allv = [m for _, v in series.values() for m in v]
    if not allv:
        raise ValueError("no results with a magnitude to plot")
    edges = np.arange(np.floor(min(allv)), np.ceil(max(allv)) + bin_width, bin_width)
    centres = (edges[:-1] + edges[1:]) / 2
    fig, (ax, ax_total, ax_norm) = plt.subplots(3, 1, figsize=(9, 13), sharex=True)
    for label, (color, v) in series.items():
        if v:
            ax.hist(v, bins=edges, histtype="step", lw=2, color=color, label=f"{label} (n={len(v):,})")
            ax_norm.hist(v, bins=edges, histtype="step", lw=2, color=color, weights=np.full(len(v), 1 / len(v)))
    ax.set_yscale("log")
    ax.set_ylabel("results" if not authoritative_only else "distinct objects")
    ax.set_title("magnitudes of results, by what they match")
    ax.legend(fontsize=8, loc="upper left")

    bottom = np.zeros(len(centres))
    for label, (color, v) in series.items():
        if label == NOT_PUTATIVE_REAL or not v:
            continue
        counts, _ = np.histogram(v, bins=edges)
        ax_total.bar(centres, counts, width=bin_width * 0.9, bottom=bottom, color=color, label=f"{label} (n={len(v):,})")
        bottom += counts
    ax_total.set_ylabel("objects" if authoritative_only else "results")
    ax_total.set_title(f"total, stacked (excluding '{NOT_PUTATIVE_REAL}', which dominates by count)")
    ax_total.legend(fontsize=8, loc="upper left")

    ax_norm.set_xlabel("median magnitude of the result's gathered points")
    ax_norm.set_ylabel("fraction of the group")
    for a_ in (ax, ax_total, ax_norm):
        a_.grid(alpha=0.25)
    return _save(fig, path)


def half_completeness_magnitude(bins):
    """The magnitude at which the recovered fraction first falls below one half, linearly
    interpolated between bin centres, or ``None`` if it never does (or never reaches half)."""
    centres = [(b[0] + b[1]) / 2 for b in bins]
    frac = [b[4] for b in bins]
    for i in range(1, len(bins)):
        if frac[i - 1] >= 0.5 > frac[i]:
            t = (frac[i - 1] - 0.5) / (frac[i - 1] - frac[i])
            return centres[i - 1] + t * (centres[i] - centres[i - 1])
    return None


def plot_completeness(completeness, path, source="synthetic", bin_width=0.5, radius=None):
    """Recovered fraction against truth magnitude, with Wilson 68% intervals, and the counts behind it."""
    plt = _plt()
    bins = bin_completeness(completeness, bin_width)
    if not bins:
        raise ValueError(f"no findable {source} objects to plot")
    centres = np.array([(b[0] + b[1]) / 2 for b in bins])
    frac = np.array([b[4] for b in bins])
    low = np.array([b[5] for b in bins])
    high = np.array([b[6] for b in bins])
    fig, (ax, ax_n) = plt.subplots(2, 1, figsize=(9, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    ax.errorbar(centres, frac, yerr=[frac - low, high - frac], fmt="o-", color="tab:purple", capsize=3)
    half = half_completeness_magnitude(bins)
    if half is not None:
        ax.axvline(half, color="k", ls=":", lw=1)
        ax.annotate(f"50% at {half:.1f}", (half, 0.52), xytext=(5, 0), textcoords="offset points", fontsize=9)
    ax.axhline(0.5, color="0.7", lw=0.8)
    ax.set_ylim(-0.03, 1.05)
    ax.set_ylabel("fraction recovered")
    n_rec = sum(r.recovered for r in completeness)
    ax.set_title(f"{source} objects recovered by the search: {n_rec:,} of {len(completeness):,} findable"
                 + (f" (matched within {radius:g}\")" if radius else ""))
    ax.grid(alpha=0.25)
    ax_n.bar(centres, [b[2] for b in bins], width=bin_width * 0.9, color="0.75", label="findable")
    ax_n.bar(centres, [b[3] for b in bins], width=bin_width * 0.9, color="tab:purple", label="recovered")
    ax_n.set_xlabel("median truth magnitude")
    ax_n.set_ylabel("objects")
    ax_n.legend(fontsize=8)
    return _save(fig, path)
