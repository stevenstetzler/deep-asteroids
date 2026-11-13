"""
Shared geometry for de-duplication and cross-matching: where linear tracklets
are at a set of times, and which pairs of them come near each other.
"""
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from deep_asteroids.ingest.fit import sep_arcsec, utc_to_tai


def exposure_times_tai(midpoints_mjd_utc):
    """The sorted distinct exposure times, as TAI MJD, from deep-db's UTC
    midpoints (find-asteroids' tracklets are in TAI)."""
    return np.array(sorted({float(utc_to_tai(m)) for m in midpoints_mjd_utc}))


def positions(ra_0, dec_0, vra, vdec, times):
    """``(ra, dec)`` arrays of shape ``(n_tracklets, n_times)``, degrees, for
    tracklets in find-asteroids' MJD-0 convention (``ra = ra_0 + vra * t``;
    ``ra_0`` is stored wrapped to [0, 360), so only differences are meaningful)."""
    t = np.asarray(times, dtype=float)[None, :]
    ra = (np.asarray(ra_0, float)[:, None] + np.asarray(vra, float)[:, None] * t) % 360.0
    dec = np.asarray(dec_0, float)[:, None] + np.asarray(vdec, float)[:, None] * t
    return ra, dec


def unit_vectors(ra, dec):
    ra, dec = np.radians(ra), np.radians(dec)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def chord(radius_arcsec):
    """The straight-line distance between unit vectors a great-circle ``radius_arcsec`` apart."""
    return 2.0 * np.sin(np.radians(radius_arcsec / 3600.0) / 2.0)


def pairs_within(ra_a, dec_a, radius_arcsec, ra_b=None, dec_b=None):
    """Index pairs ``(i, j)``, an ``(n, 2)`` int array, of tracklets that are
    within ``radius_arcsec`` of each other at **any** of the times (the
    columns of ``ra_*``/``dec_*``): between the ``a`` set and itself (``i < j``)
    if no ``b`` set is given, else between ``a`` (``i``) and ``b`` (``j``).

    A pair whose mean separation over the times is below the radius must be
    below it at one of them, so this loses no pair that could pass a mean-
    separation test; it only avoids evaluating all pairs. One KD-tree per time."""
    r = chord(radius_arcsec)
    found = []
    for k in range(ra_a.shape[1]):
        tree_a = cKDTree(unit_vectors(ra_a[:, k], dec_a[:, k]))
        if ra_b is None:
            found.append(tree_a.query_pairs(r, output_type="ndarray"))
        else:
            tree_b = cKDTree(unit_vectors(ra_b[:, k], dec_b[:, k]))
            m = tree_a.sparse_distance_matrix(tree_b, r, output_type="ndarray")
            found.append(np.stack([m["i"], m["j"]], axis=1))
    found = [f for f in found if len(f)]
    if not found:
        return np.empty((0, 2), dtype=np.int64)
    return np.unique(np.concatenate(found).astype(np.int64), axis=0)


def pair_separations(ra_a, dec_a, ra_b, dec_b, pairs):
    """Great-circle separations (arcsec), shape ``(n_pairs, n_times)``, of the
    tracklet pairs ``pairs`` (indices into ``a`` and ``b``)."""
    return sep_arcsec(ra_a[pairs[:, 0]], dec_a[pairs[:, 0]], ra_b[pairs[:, 1]], dec_b[pairs[:, 1]])


def cluster_results(ra, dec, merge_radius_arcsec):
    """Cluster tracklets given their positions ``(n, n_times)``: ``labels``, one per
    tracklet, equal for redetections of one object."""
    n = ra.shape[0]
    pairs = pairs_within(ra, dec, merge_radius_arcsec)
    if len(pairs):
        mean_sep = pair_separations(ra, dec, ra, dec, pairs).mean(axis=1)
        pairs = pairs[mean_sep < merge_radius_arcsec]
    graph = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)) if len(pairs) else \
        coo_matrix((n, n))
    return connected_components(graph, directed=False)[1]
