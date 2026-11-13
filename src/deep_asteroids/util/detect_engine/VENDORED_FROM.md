# Vendored from `salad/src/salad/detect/engine/`

`detect_and_measure.cc` and `detect_common.h` are vendored **byte-for-byte**,
unchanged, from `salad/src/salad/detect/engine/` as of 2026-11-12. They are
the real source-detection engine salad's searches used: a port of LSST's
`SourceDetectionTask.detectFootprints()` (PSF-matched-filter smoothing,
thresholding, peak finding, temporary local background, mask census) plus
forced PSF photometry at each peak, with no Python or LSST runtime
dependency once built. See that directory's `README.md` for what it does and
how it was validated against the LSST task's own output peak for peak.

The sibling `../cutout_engine/` vendors the forced-photometry/cutout
binary and a trimmed `detect_common.h`; the two directories deliberately do
not share a header (this one needs the whole detection algorithm, the other
would only pay for its includes). The two other pieces of salad's `engine/`
are not vendored: `detect_min.cc` (no ra/dec, so unusable for a search) and
`detect.sh`/`detect_csv_to_table.py` (a shell pipe converting the CSV to an
ecsv table; `../detect.py` parses the CSV and metadata directly).

`build.sh` is **adapted, not copied verbatim**: salad's loops over three
binaries sharing one flag set; this only builds `detect_and_measure`, with
the exact same include/link flags. (The linker prints a harmless warning that
`libfftw3f.so.3` is not found for `libmeas_extensions_psfex.so`; the binary
runs.)

## Building

Requires the `opt_lsst` LSST Science Pipelines environment sourced (for
`g++` and the EUPS `*_DIR` variables `build.sh` reads), **once**, at build
time only. On Klone, inside a Slurm job (about 1.5 minutes):

```bash
cd src/deep_asteroids/util/detect_engine
source /mmfs1/gscratch/dirac/shared/opt_lsst/bin/opt_lsst.sh && opt_lsst setup w_2023_38
bash build.sh
```

The binary is git-ignored.

## Verified

Built against `w_2023_38` on 2026-11-12 and run on one real DECam
`differenceExp` (visit 845580, detector N11, night 20190402, field A0b),
its CSV and metadata output was **byte-identical** to salad's own
already-built `detect_and_measure` (125 peaks): same size binary (227,984
bytes), same peaks, same `MJD`. That run took 3.9 s and 284 MB.

The engine's `MJD` is the VisitInfo date as UTC **plus 0.5 s** (a DECam
shutter latency correction, in `detect_common.h`'s `computeMetadata()`),
which deep-db's exposure midpoint does not include. `search/catalog.py`
therefore labels each exposure from deep-db's midpoint plus a
`--shutter-offset-seconds` constant instead, and only checks the engine's
value against it.
