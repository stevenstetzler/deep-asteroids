"""
Vectorized linear sky-plane tracklet fits.

A tracklet is

    ra(t)  = ra_epoch  + vra  * (t - epoch)
    dec(t) = dec_epoch + vdec * (t - epoch)

with angles in degrees, rates in deg/day, and ``vra`` the plain dRA/dt (no
cos(dec) factor -- find-asteroids' convention). ``epoch`` is the mean time
of the fitted points, which makes the fitted position uncorrelated with
the rate and avoids the precision loss of extrapolating to MJD 0 (where
find-asteroids' own ``Tracklet.ra_0``/``dec_0`` live -- see
``to_mjd0()``).

``fit_linear_tracklets`` fits every group of points in one pass with
ordinary least squares (no per-group Python loop), so a whole deep-db
table fits in seconds. Adapted from tracklet-db's ``fit.py``.
"""
import numpy as np


def wrap180(x):
    """Wrap angles (deg) into [-180, 180)."""
    return (np.asarray(x, dtype=float) + 180.0) % 360.0 - 180.0


def utc_to_tai(mjd_utc):
    """UTC MJD -> TAI MJD (find-asteroids fits and predicts in TAI)."""
    from astropy.time import Time

    mjd_utc = np.asarray(mjd_utc, dtype=float)
    if mjd_utc.size == 0:
        return mjd_utc
    return Time(mjd_utc, format="mjd", scale="utc").tai.mjd


def fit_linear_tracklets(group, t, ra, dec):
    """Least-squares fit of ra(t), dec(t), linear in t, independently per group.

    group: integer label per point (any order); t: times (MJD); ra, dec: deg
    (any RA wrapping -- RA is unwrapped about each group's first point, so a
    track crossing RA=0 fits correctly).

    Returns a dict of per-group arrays, sorted by group label: ``group``,
    ``n`` (points), ``n_times`` (distinct times), ``epoch``, ``ra_epoch`` (in
    [0, 360)), ``dec_epoch``, ``vra``, ``vdec``, ``t_min``, ``t_max``,
    ``max_residual_arcsec`` (largest on-sky residual from the fit). A group
    with fewer than 2 distinct times can't be fit: its ``epoch``/position/
    rate/residual entries are NaN (``n``/``t_min``/``t_max`` are still set).
    """
    group = np.asarray(group)
    t = np.asarray(t, dtype=float)
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    if not (len(group) == len(t) == len(ra) == len(dec)):
        raise ValueError("group, t, ra and dec must all have the same length")

    labels, idx, n = np.unique(group, return_inverse=True, return_counts=True)
    ng = len(labels)

    def gsum(x):
        return np.bincount(idx, weights=x, minlength=ng)

    first = np.full(ng, -1)
    first[idx[::-1]] = np.arange(len(idx))[::-1]
    ra_ref = ra[first]
    x = wrap180(ra - ra_ref[idx])

    epoch = gsum(t) / n
    dt = t - epoch[idx]
    sdt2 = gsum(dt * dt)
    fittable = sdt2 > 0  # >= 2 distinct times
    with np.errstate(invalid="ignore", divide="ignore"):
        vra = np.where(fittable, gsum(dt * x) / sdt2, np.nan)
        vdec = np.where(fittable, gsum(dt * dec) / sdt2, np.nan)
    x_mean = gsum(x) / n
    dec_mean = gsum(dec) / n

    with np.errstate(invalid="ignore"):
        rx = (x - x_mean[idx] - vra[idx] * dt) * np.cos(np.radians(dec))
        ry = dec - dec_mean[idx] - vdec[idx] * dt
        residual = np.hypot(rx, ry) * 3600.0
    max_residual = np.full(ng, -np.inf)
    np.maximum.at(max_residual, idx, np.where(np.isfinite(residual), residual, -np.inf))
    max_residual[~fittable] = np.nan

    t_min = np.full(ng, np.inf)
    t_max = np.full(ng, -np.inf)
    np.minimum.at(t_min, idx, t)
    np.maximum.at(t_max, idx, t)

    order = np.lexsort((t, idx))
    t_sorted, idx_sorted = t[order], idx[order]
    new_time = np.ones(len(t), dtype=bool)
    new_time[1:] = (idx_sorted[1:] != idx_sorted[:-1]) | (t_sorted[1:] != t_sorted[:-1])
    n_times = np.bincount(idx_sorted[new_time], minlength=ng)

    nan = np.full(ng, np.nan)
    return {
        "group": labels,
        "n": n,
        "n_times": n_times,
        "epoch": np.where(fittable, epoch, nan),
        "ra_epoch": np.where(fittable, (ra_ref + x_mean) % 360.0, nan),
        "dec_epoch": np.where(fittable, dec_mean, nan),
        "vra": vra,
        "vdec": vdec,
        "t_min": t_min,
        "t_max": t_max,
        "max_residual_arcsec": max_residual,
    }


def predict(epoch, ra_epoch, dec_epoch, vra, vdec, t):
    """Position (ra in [0, 360), dec) of a tracklet at time(s) ``t``."""
    dt = np.asarray(t, dtype=float) - epoch
    return (ra_epoch + vra * dt) % 360.0, dec_epoch + vdec * dt


def to_mjd0(epoch, ra_epoch, dec_epoch, vra, vdec):
    """(ra_0, dec_0): the tracklet's position extrapolated to MJD 0 -- the
    convention find-asteroids' ``Tracklet.ra_0``/``dec_0`` use (in the same
    time scale as ``epoch``). ``ra_0`` is not wrapped: it's a line
    intercept, not a sky position."""
    return ra_epoch - vra * epoch, dec_epoch - vdec * epoch


def tangent_plane_weighted_mean(ra1, dec1, ra2, dec2, w2):
    """Per-row weighted mean of two nearby sky positions, ``(p1 + w2*p2) /
    (1 + w2)``, computed in the tangent plane about ``p1`` (gnomonic), so
    it's correct across RA=0 and away from the equator. ``w2 = 1`` gives
    the midpoint."""
    ra1, dec1, ra2, dec2 = (np.radians(np.asarray(a, dtype=float)) for a in (ra1, dec1, ra2, dec2))
    w2 = np.asarray(w2, dtype=float)
    # gnomonic projection of p2 about p1
    cos_c = np.sin(dec1) * np.sin(dec2) + np.cos(dec1) * np.cos(dec2) * np.cos(ra2 - ra1)
    xi = np.cos(dec2) * np.sin(ra2 - ra1) / cos_c
    eta = (np.cos(dec1) * np.sin(dec2) - np.sin(dec1) * np.cos(dec2) * np.cos(ra2 - ra1)) / cos_c
    f = w2 / (1.0 + w2)
    xi, eta = f * xi, f * eta
    # inverse gnomonic about p1
    rho = np.hypot(xi, eta)
    c = np.arctan(rho)
    with np.errstate(invalid="ignore", divide="ignore"):
        dec = np.where(
            rho > 0,
            np.arcsin(np.cos(c) * np.sin(dec1) + eta * np.sin(c) * np.cos(dec1) / np.where(rho > 0, rho, 1)),
            dec1,
        )
    ra = ra1 + np.arctan2(xi * np.sin(c), rho * np.cos(dec1) * np.cos(c) - eta * np.sin(dec1) * np.sin(c))
    return np.degrees(ra) % 360.0, np.degrees(dec)


def sep_arcsec(ra1, dec1, ra2, dec2):
    """Great-circle separation (arcsec), haversine."""
    ra1, dec1, ra2, dec2 = (np.radians(np.asarray(a, dtype=float)) for a in (ra1, dec1, ra2, dec2))
    a = np.sin((dec2 - dec1) / 2) ** 2 + np.cos(dec1) * np.cos(dec2) * np.sin((ra2 - ra1) / 2) ** 2
    return np.degrees(2 * np.arcsin(np.minimum(1.0, np.sqrt(a)))) * 3600.0
