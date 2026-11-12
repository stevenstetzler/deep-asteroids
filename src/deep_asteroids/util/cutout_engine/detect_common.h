// detect_common.h -- trimmed to exactly what cutout_and_photometry.cc
// uses, from salad's src/salad/detect/engine/detect_common.h (which holds
// the full SourceDetectionTask.detectFootprints() port shared by
// detect_min.cc/detect_and_measure.cc -- the detection algorithm itself).
// cutout_and_photometry.cc never detects anything: it only reads an
// Exposure's own pixels/Psf/PhotoCalib at caller-supplied positions, so
// it needs none of that -- just the Exposure/MaskedImage type aliases,
// the stdout-suppression RAII helper (LSST's own logging can corrupt a
// CSV piped to stdout during readFits()), and the CSV-splitting helper.
// See VENDORED_FROM.md for what's dropped and why this isn't a byte-for-
// byte copy, unlike cutout_and_photometry.cc itself.
#pragma once

#include <fcntl.h>
#include <filesystem>  // cutout_and_photometry.cc's main() calls create_directories() directly; relies on this transitively
#include <iostream>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>

#include "lsst/afw/image.h"
#include "lsst/afw/detection/Psf.h"

namespace detect_common {

namespace image = lsst::afw::image;

using Pixel = float;
using Exposure = image::Exposure<Pixel>;
using MaskedImage = image::MaskedImage<Pixel>;

/// RAII helper: redirects the process's real stdout (fd 1) to /dev/null for
/// its lifetime, restoring the original fd on destruction. Needed because
/// LSST's own logging (lsst.log / log4cxx's default console appender)
/// writes WARN-level messages -- e.g. ExposureFitsReader's "Could not read
/// component SUMMARY_STATS" on a real differenceExp -- directly to fd 1,
/// bypassing this program's own std::cout/std::cerr entirely (confirmed
/// empirically: it lands in stdout even under `2>/dev/null`, and even in
/// --quiet mode, since it never goes through an iostream call this program
/// controls). That corrupts a CSV written to stdout via `--csv -`, so
/// callers wrap just the readFits() window in one of these -- normal
/// (file-output) runs never construct one, so every diagnostic still
/// prints exactly as before there.
class StdoutSuppressor {
public:
    StdoutSuppressor() {
        std::cout.flush();
        savedFd_ = dup(STDOUT_FILENO);
        int devNull = open("/dev/null", O_WRONLY);
        dup2(devNull, STDOUT_FILENO);
        close(devNull);
    }
    ~StdoutSuppressor() {
        std::cout.flush();
        dup2(savedFd_, STDOUT_FILENO);
        close(savedFd_);
    }
    StdoutSuppressor(StdoutSuppressor const&) = delete;
    StdoutSuppressor& operator=(StdoutSuppressor const&) = delete;

private:
    int savedFd_;
};

inline std::vector<std::string> splitCsv(std::string const& s) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ',')) {
        if (!item.empty()) out.push_back(item);
    }
    return out;
}

}  // namespace detect_common
