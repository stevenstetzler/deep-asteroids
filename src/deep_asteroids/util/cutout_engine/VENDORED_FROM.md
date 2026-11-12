# Vendored from `salad/src/salad/detect/engine/`

`cutout_and_photometry.cc` is vendored **byte-for-byte**, unchanged, from
`salad/src/salad/detect/engine/cutout_and_photometry.cc` as of 2026-11-12.
See that file's own top comment for what it does (forced PSF photometry +
a windowed pixel cutout at caller-supplied sky/pixel positions), and
`salad/src/salad/detect/engine/README.md`'s "Note on precision" for the
`skyToPixel()`/CSV-rounding caveats that still apply here unchanged.

**`detect_common.h` is *not* a byte-for-byte copy** -- it's trimmed to
exactly what `cutout_and_photometry.cc` actually uses: the
`Exposure`/`MaskedImage`/`Pixel` type aliases, the `StdoutSuppressor` RAII
helper, and `splitCsv()`. Salad's own `detect_common.h` is shared with
`detect_min.cc`/`detect_and_measure.cc` (also vendored there, **not**
vendored here -- source detection is out of scope for deep-asteroids;
#5/this issue only need forced photometry + cutouts at *known* positions)
and carries the entire `SourceDetectionTask.detectFootprints()` port
(convolution, thresholding, peak re-detection, local background, mask
census, metadata) that `cutout_and_photometry.cc` never calls. Keeping
that dead code here would also pull in `lsst/afw/detection.h`,
`lsst/afw/math/*`, `lsst/daf/base/DateTime.h` and `lsst/pex/exceptions.h`
for nothing. If a future "detect" phase needs the real detection engine,
vendor salad's own `detect_common.h`/`detect_min.cc`/`detect_and_measure.cc`
separately rather than growing this one back out.

`build.sh` is **adapted, not copied verbatim**: salad's version loops
over three binaries (`detect_min`, `detect_and_measure`,
`cutout_and_photometry`) that happen to share one flag set; this only
builds `cutout_and_photometry`, with the exact same include/link flags
(see this file's own top comment for why the full `meas_algorithms`/
`meas_base`/`meas_extensions_psfex`/`psfex` link set is still needed even
though this binary has no detection algorithm of its own -- Psf
deserialization needs those libraries *loaded*, not just linked against
by something else). No other behavior differs from salad's own flags.

## Building

Requires the `opt_lsst` LSST Science Pipelines environment sourced (for
`g++` and the EUPS `*_DIR` variables `build.sh` reads) -- **once**, at
build time only; the resulting binary has no LSST/Python runtime
dependency. On klone, `opt_lsst` lives at
`/mmfs1/gscratch/dirac/shared/opt_lsst` (`w_2023_38` installed there as
of 2026-11; confirmed to build and run correctly -- see salad's own
`VENDORED_FROM.md` for the same finding against the detection binaries):

```bash
cd src/deep_asteroids/util/cutout_engine
source /mmfs1/gscratch/dirac/shared/opt_lsst/bin/opt_lsst.sh && opt_lsst setup w_2023_38  # or whichever tag matches production
bash build.sh
```

This produces `cutout_and_photometry` in this directory (git-ignored),
which `../cutout.py`'s `LSSTCutoutPhotometry` then invokes via
subprocess -- no shell wrapper needed, since (unlike `detect.sh`'s
ecsv-conversion step in salad) there's no post-processing between the
binary's own CSV output and what the Python wrapper reads.

**The binary/API has no long-term stability guarantee** across LSST
weekly tags (salad's own note) -- re-validate forced photometry against
a known image/position pair after building against a different tag,
don't assume a previous build's results still apply. The validation in
#8 used the same DECam `differenceExp` salad's own `VENDORED_FROM.md`
cites (`DEEP/20190402/A0b`, visit 845580, detector S25).
