#!/usr/bin/env bash
# Build cutout_and_photometry.cc against the LSST afw + meas_algorithms/
# meas_base C++ libraries directly -- no Python involved in either
# building or running the result. Needs the opt_lsst env sourced first
# (for EUPS's <PRODUCT>_DIR env vars below and for g++).
#
# Adapted from salad's src/salad/detect/engine/build.sh, which loops over
# three binaries sharing one flag set (detect_min/detect_and_measure also
# vendored there; out of scope here, see VENDORED_FROM.md) -- this only
# builds cutout_and_photometry, but keeps the exact same include/link
# flags: meas_algorithms/meas_base/meas_extensions_psfex/psfex are needed
# here too, even though cutout_and_photometry.cc has no detection
# algorithm of its own, so that the exposure's real, persisted Psf object
# (concretely an lsst.meas.algorithms.KernelPsf) can be deserialized at
# all -- afw's polymorphic table-persistence system resolves a Psf's
# concrete C++ type via a factory registered by a static initializer in
# whichever library defines it, so that library must actually be
# *loaded* into the process (not just declared) for ExposureF::readFits
# to succeed on it.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CONDA_LIB="$(dirname "$(dirname "$(command -v g++)")")/lib"  # third-party libs (boost, cfitsio, gsl, log4cxx, fftw3, ...) live here, not under any EUPS product

g++ -std=c++17 -O2 \
    -I"${AFW_DIR}/include" \
    -I"${MEAS_ALGORITHMS_DIR}/include" \
    -I"${MEAS_BASE_DIR}/include" \
    -I"${MEAS_EXTENSIONS_PSFEX_DIR}/include" \
    -I"${GEOM_DIR}/include" \
    -I"${DAF_BASE_DIR}/include" \
    -I"${PEX_EXCEPTIONS_DIR}/include" \
    -I"${PEX_CONFIG_DIR}/include" \
    -I"${CPPUTILS_DIR}/include" \
    -I"${BASE_DIR}/include" \
    -I"${LOG_DIR}/include" \
    -I"${SPHGEOM_DIR}/include" \
    -I"${ASTSHIM_DIR}/include" \
    -I"${UTILS_DIR}/include" \
    -I"${CONDA_LIB}/../include" \
    -I"${CONDA_LIB}/../include/eigen3" \
    cutout_and_photometry.cc -o cutout_and_photometry \
    -L"${AFW_DIR}/lib" -Wl,-rpath,"${AFW_DIR}/lib" \
    -L"${MEAS_ALGORITHMS_DIR}/lib" -Wl,-rpath,"${MEAS_ALGORITHMS_DIR}/lib" \
    -L"${MEAS_BASE_DIR}/lib" -Wl,-rpath,"${MEAS_BASE_DIR}/lib" \
    -L"${MEAS_EXTENSIONS_PSFEX_DIR}/lib" -Wl,-rpath,"${MEAS_EXTENSIONS_PSFEX_DIR}/lib" \
    -L"${PSFEX_DIR}/lib" -Wl,-rpath,"${PSFEX_DIR}/lib" \
    -L"${GEOM_DIR}/lib" -Wl,-rpath,"${GEOM_DIR}/lib" \
    -L"${DAF_BASE_DIR}/lib" -Wl,-rpath,"${DAF_BASE_DIR}/lib" \
    -L"${PEX_EXCEPTIONS_DIR}/lib" -Wl,-rpath,"${PEX_EXCEPTIONS_DIR}/lib" \
    -L"${CPPUTILS_DIR}/lib" -Wl,-rpath,"${CPPUTILS_DIR}/lib" \
    -L"${BASE_DIR}/lib" -Wl,-rpath,"${BASE_DIR}/lib" \
    -L"${LOG_DIR}/lib" -Wl,-rpath,"${LOG_DIR}/lib" \
    -L"${SPHGEOM_DIR}/lib" -Wl,-rpath,"${SPHGEOM_DIR}/lib" \
    -L"${ASTSHIM_DIR}/lib" -Wl,-rpath,"${ASTSHIM_DIR}/lib" \
    -L"${CONDA_LIB}" -Wl,-rpath,"${CONDA_LIB}" \
    -Wl,--start-group \
    -lmeas_extensions_psfex -lpsfex -lmeas_algorithms -lmeas_base \
    -lafw -lgeom -ldaf_base -lpex_exceptions -lcpputils -lbase -llog -lsphgeom -lastshim \
    -lgsl -lgslcblas -last -last_grf_2.0 -last_grf_3.2 -last_grf_5.6 -last_grf3d -last_err -lMinuit2 -llog4cxx -lcfitsio -lfftw3 \
    -Wl,--end-group

echo "built ./cutout_and_photometry"
