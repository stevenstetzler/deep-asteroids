import numpy as np
import pytest

from deep_asteroids.ingest.fit import (
    fit_linear_tracklets,
    predict,
    sep_arcsec,
    tangent_plane_weighted_mean,
    to_mjd0,
    utc_to_tai,
    wrap180,
)


def _line(t, ra_epoch, dec_epoch, vra, vdec, epoch):
    return ra_epoch + vra * (t - epoch), dec_epoch + vdec * (t - epoch)


def test_recovers_two_lines_exactly_and_uses_mean_epoch():
    t1 = np.array([58600.10, 58600.15, 58600.20, 58600.30])
    t2 = np.array([58601.0, 58601.1, 58601.2])
    ra1, dec1 = _line(t1, 215.0, -13.0, 0.25, -0.05, t1.mean())
    ra2, dec2 = _line(t2, 10.0, 5.0, -0.1, 0.02, t2.mean())
    # interleave the groups to check order-independence
    group = np.r_[np.full(4, 7), np.full(3, 3)][[0, 4, 1, 5, 2, 6, 3]]
    t = np.r_[t1, t2][[0, 4, 1, 5, 2, 6, 3]]
    ra = np.r_[ra1, ra2][[0, 4, 1, 5, 2, 6, 3]]
    dec = np.r_[dec1, dec2][[0, 4, 1, 5, 2, 6, 3]]

    fit = fit_linear_tracklets(group, t, ra, dec)

    assert list(fit["group"]) == [3, 7]
    np.testing.assert_allclose(fit["epoch"], [t2.mean(), t1.mean()])
    np.testing.assert_allclose(fit["ra_epoch"], [10.0, 215.0], atol=1e-10)
    np.testing.assert_allclose(fit["dec_epoch"], [5.0, -13.0], atol=1e-10)
    np.testing.assert_allclose(fit["vra"], [-0.1, 0.25], atol=1e-9)
    np.testing.assert_allclose(fit["vdec"], [0.02, -0.05], atol=1e-9)
    np.testing.assert_array_equal(fit["n"], [3, 4])
    np.testing.assert_array_equal(fit["n_times"], [3, 4])
    np.testing.assert_allclose(fit["t_min"], [t2.min(), t1.min()])
    np.testing.assert_allclose(fit["t_max"], [t2.max(), t1.max()])
    assert np.all(fit["max_residual_arcsec"] < 1e-6)


def test_ra_wrap_crossing():
    t = np.array([0.0, 0.5, 1.0, 1.5])
    ra = np.array([359.8, 359.9, 0.0, 0.1])  # +0.2 deg/day, crossing 0
    fit = fit_linear_tracklets(np.zeros(4, int), t, ra, np.zeros(4))
    np.testing.assert_allclose(fit["vra"], [0.2], atol=1e-10)
    np.testing.assert_allclose(fit["ra_epoch"], [359.95], atol=1e-10)
    assert fit["max_residual_arcsec"][0] < 1e-6


def test_residual_reports_worst_point_on_sky():
    t = np.array([0.0, 1.0, 2.0, 3.0])
    dec = np.full(4, 60.0)
    ra = np.array([10.0, 10.1, 10.2, 10.3])
    ra[1] += 1.0 / 3600  # 1" in RA at dec 60 is 0.5" on sky (before the fit absorbs part of it)
    fit = fit_linear_tracklets(np.zeros(4, int), t, ra, dec)
    pred_ra, pred_dec = predict(fit["epoch"][0], fit["ra_epoch"][0], fit["dec_epoch"][0],
                                fit["vra"][0], fit["vdec"][0], t)
    expected = sep_arcsec(ra, dec, pred_ra, pred_dec).max()
    np.testing.assert_allclose(fit["max_residual_arcsec"], [expected], rtol=1e-3)
    assert 0.1 < expected < 0.5


def test_too_few_distinct_times_is_nan_not_dropped():
    group = np.array([1, 2, 2, 3, 3, 3])
    t = np.array([5.0, 6.0, 6.0, 7.0, 7.1, 7.2])  # group 1: one point; group 2: two points, same time
    ra = np.array([1.0, 2.0, 2.0001, 3.0, 3.01, 3.02])
    dec = np.zeros(6)
    fit = fit_linear_tracklets(group, t, ra, dec)
    assert list(fit["group"]) == [1, 2, 3]
    np.testing.assert_array_equal(fit["n"], [1, 2, 3])
    np.testing.assert_array_equal(fit["n_times"], [1, 1, 3])
    for key in ("epoch", "ra_epoch", "dec_epoch", "vra", "vdec", "max_residual_arcsec"):
        assert np.isnan(fit[key][:2]).all(), key
        assert np.isfinite(fit[key][2]), key
    np.testing.assert_allclose(fit["t_min"], [5.0, 6.0, 7.0])


def test_empty_input():
    fit = fit_linear_tracklets(np.array([], int), np.array([]), np.array([]), np.array([]))
    assert len(fit["group"]) == 0


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        fit_linear_tracklets([1, 2], [0.0], [0.0, 1.0], [0.0, 1.0])


def test_to_mjd0_matches_find_asteroids_convention():
    epoch, ra_epoch, dec_epoch, vra, vdec = 58600.5, 215.0, -13.0, 0.25, -0.05
    ra_0, dec_0 = to_mjd0(epoch, ra_epoch, dec_epoch, vra, vdec)
    t = 58601.0
    # find-asteroids predicts ra = ra_0 + vra * t
    np.testing.assert_allclose((ra_0 + vra * t) % 360, predict(epoch, ra_epoch, dec_epoch, vra, vdec, t)[0])
    np.testing.assert_allclose(dec_0 + vdec * t, predict(epoch, ra_epoch, dec_epoch, vra, vdec, t)[1])


def test_utc_to_tai_offset_is_37_seconds_in_survey_era():
    np.testing.assert_allclose((utc_to_tai([58600.0]) - 58600.0) * 86400, [37.0], atol=1e-6)
    assert utc_to_tai([]).size == 0


def test_wrap180():
    np.testing.assert_allclose(wrap180([0, 180, 190, -190, 359]), [0, -180, -170, 170, -1])


@pytest.mark.parametrize("ra", [10.0, 359.9995])
def test_tangent_plane_weighted_mean(ra):
    # companion 2" north of the primary (ra=359.9995 puts the pair across RA=0 for a 2" E-W case below)
    ra1, dec1 = np.array([ra]), np.array([-13.0])
    ra2, dec2 = ra1, dec1 + 2.0 / 3600
    mid_ra, mid_dec = tangent_plane_weighted_mean(ra1, dec1, ra2, dec2, 1.0)
    np.testing.assert_allclose(sep_arcsec(ra1, dec1, mid_ra, mid_dec), [1.0], rtol=1e-6)
    np.testing.assert_allclose(sep_arcsec(ra2, dec2, mid_ra, mid_dec), [1.0], rtol=1e-6)

    w = 10 ** (-0.4 * 0.5)  # companion 0.5 mag fainter
    pc_ra, pc_dec = tangent_plane_weighted_mean(ra1, dec1, ra2, dec2, w)
    np.testing.assert_allclose(sep_arcsec(ra1, dec1, pc_ra, pc_dec), [2.0 * w / (1 + w)], rtol=1e-6)

    # east-west pair straddling RA=0
    ra3 = (ra1 + 2.0 / 3600 / np.cos(np.radians(dec1))) % 360
    mid_ra, mid_dec = tangent_plane_weighted_mean(ra1, dec1, ra3, dec1, 1.0)
    np.testing.assert_allclose(sep_arcsec(ra1, dec1, mid_ra, mid_dec), [1.0], rtol=1e-4)
    assert 0 <= mid_ra[0] < 360


def test_tangent_plane_weighted_mean_identical_points():
    ra, dec = tangent_plane_weighted_mean([5.0], [1.0], [5.0], [1.0], 1.0)
    np.testing.assert_allclose([ra[0], dec[0]], [5.0, 1.0])
