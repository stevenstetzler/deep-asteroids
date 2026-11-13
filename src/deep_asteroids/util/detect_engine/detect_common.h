// detect_common.h -- the detection algorithm shared by detect_min.cc and
// detect_and_measure.cc, factored out so the two binaries run the exact
// same compiled logic (not a hand-copied second version that could drift).
// See detect_min.cc's own top comment for what this ports and from where;
// this header holds everything from "read the exposure's mask/psf" through
// "final, grown FootprintSets per polarity" -- i.e. all of
// SourceDetectionTask.detectFootprints() except the CLI/output layer, which
// stays per-binary since detect_min and detect_and_measure write different
// columns.
#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <fcntl.h>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <unistd.h>
#include <vector>

#include "lsst/afw/image.h"
#include "lsst/afw/detection.h"
#include "lsst/afw/detection/Psf.h"
#include "lsst/afw/math/Background.h"
#include "lsst/afw/math/ConvolveImage.h"
#include "lsst/afw/math/FunctionLibrary.h"
#include "lsst/afw/math/Kernel.h"
#include "lsst/afw/math/Statistics.h"
#include "lsst/daf/base/DateTime.h"
#include "lsst/pex/exceptions.h"

namespace detect_common {

namespace image = lsst::afw::image;
namespace detection = lsst::afw::detection;
namespace math = lsst::afw::math;

using Pixel = float;
using Exposure = image::Exposure<Pixel>;
using MaskedImage = image::MaskedImage<Pixel>;

struct Options {
    std::string path;
    double thresholdSigma = 5.0;
    int npixMin = 1;
    std::string thresholdPolarity = "positive";  // "positive", "negative", or "both" -- matches SourceDetectionConfig.thresholdPolarity
    double nSigmaToGrow = 2.4;
    bool isotropicGrow = true;
    double nSigmaForKernel = 7.0;
    int localBackgroundBinSize = 64;
    bool doSmooth = true;
    bool doTempLocalBackground = true;
    double psfSigmaOverride = -1.0;  // < 0 -> measure from the exposure's own Psf, matching detect()
    std::vector<std::string> excludeMaskPlanes = {
        "EDGE", "SAT", "SUSPECT", "BAD", "NO_DATA", "STREAK", "CROSSTALK", "NOT_DEBLENDED",
    };
    std::string csvPath;
    std::string heavyPath;
    std::string backgroundPath;
    std::string middlePath;
    std::string middleRawPath;
    std::string metadataPath;  // '-' for stdout, matching csvPath's convention
    bool quiet = false;
};

/// RAII helper: redirects the process's real stdout (fd 1) to /dev/null for
/// its lifetime, restoring the original fd on destruction. Needed because
/// LSST's own logging (lsst.log / log4cxx's default console appender)
/// writes WARN-level messages -- e.g. ExposureFitsReader's "Could not read
/// component SUMMARY_STATS" on a real differenceExp -- directly to fd 1,
/// bypassing this program's own std::cout/std::cerr entirely (confirmed
/// empirically: it lands in stdout even under `2>/dev/null`, and even in
/// --quiet mode, since it never goes through an iostream call this program
/// controls). That corrupts a CSV written to stdout via `--csv -`, so
/// callers wrap just the readFits()+runDetection() window in one of these
/// when piping -- normal (file-output) runs never construct one, so every
/// diagnostic still prints exactly as before there.
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

inline int calculateKernelSize(double sigma, double nSigmaForKernel) {
    // Same rounding as SourceDetectionTask.calculateKernelSize: round up to
    // the nearest odd integer.
    return (static_cast<int>(sigma * nSigmaForKernel + 0.5) / 2) * 2 + 1;
}

/// Port of SourceDetectionTask.convolveImage(): Gaussian-approximate the
/// PSF and convolve, returning the convolved image with the
/// half-kernel-width edges trimmed off (the "middle" region actually
/// searched for footprints).
inline MaskedImage convolveImage(MaskedImage const& maskedImage, double sigma, double nSigmaForKernel, bool doSmooth) {
    if (!doSmooth) {
        return MaskedImage(maskedImage, true);  // deep copy, matching maskedImage.Factory(maskedImage, deep=True)
    }
    int kWidth = calculateKernelSize(sigma, nSigmaForKernel);
    math::GaussianFunction1<double> gaussFunc(sigma);
    math::SeparableKernel gaussKernel(kWidth, kWidth, gaussFunc, gaussFunc);

    MaskedImage convolvedImage(maskedImage.getBBox());
    math::convolve(convolvedImage, maskedImage, gaussKernel, math::ConvolutionControl());

    lsst::geom::Box2I goodBBox = gaussKernel.shrinkBBox(convolvedImage.getBBox());
    return MaskedImage(convolvedImage, goodBBox, image::PARENT, false);
}

/// Port of SourceDetectionTask.removeBadPixels(): zero the (convolved)
/// image value at any pixel flagged in excludeMaskPlanes, so such pixels
/// can never cross a positive threshold.
inline void removeBadPixels(MaskedImage& middle, std::vector<std::string> const& excludeMaskPlanes) {
    if (excludeMaskPlanes.empty()) return;
    auto badPixelMask = image::Mask<image::MaskPixel>::getPlaneBitMask(excludeMaskPlanes);
    auto maskArray = middle.getMask()->getArray();
    auto imageArray = middle.getImage()->getArray();
    auto shape = maskArray.getShape();
    for (int y = 0; y < shape[0]; ++y) {
        for (int x = 0; x < shape[1]; ++x) {
            if (maskArray[y][x] & badPixelMask) {
                imageArray[y][x] = 0;
            }
        }
    }
}

/// Port of SourceDetectionTask.updatePeaks(): re-detect within each
/// footprint's own bbox on the (locally background-subtracted) image, and
/// replace that footprint's peaks with the new ones that fall inside it --
/// footprints with <= nPeaksMaxSimple(=1) peaks are left untouched, and a
/// footprint with no re-detected peaks keeps only its original brightest one.
inline void updatePeaks(detection::FootprintSet& fpSet, MaskedImage const& image, detection::Threshold const& threshold,
                         int npixMin) {
    for (auto const& footprint : *fpSet.getFootprints()) {
        auto& oldPeaks = footprint->getPeaks();
        if (oldPeaks.size() <= 1) continue;

        MaskedImage sub(image, footprint->getBBox(), image::PARENT, false);
        detection::FootprintSet subSet(sub, threshold, "", npixMin);

        detection::PeakCatalog newPeaks(oldPeaks.getTable());
        for (auto const& subFp : *subSet.getFootprints()) {
            for (auto const& peak : subFp->getPeaks()) {
                if (footprint->contains(peak.getI())) {
                    newPeaks.push_back(peak);
                }
            }
        }
        if (!newPeaks.empty()) {
            oldPeaks.clear();
            for (auto const& p : newPeaks) oldPeaks.push_back(p);
        } else if (oldPeaks.size() > 1) {
            // Keep only the brightest (first -- afw sorts a Footprint's
            // peaks by decreasing value at construction time) original peak.
            detection::PeakCatalog kept(oldPeaks.getTable());
            kept.push_back(oldPeaks.front());
            oldPeaks.clear();
            oldPeaks.push_back(kept.front());
        }
    }
}

/// Port of the relevant half of SourceDetectionTask.applyThreshold(): detect
/// footprints of one polarity ("positive" parity=true finds pixels above
/// +threshold; "negative" parity=false finds pixels below -threshold).
inline std::unique_ptr<detection::FootprintSet> detectPolarity(MaskedImage const& middle, lsst::geom::Box2I const& region,
                                                                 double thresholdSigma, int npixMin, bool positive) {
    detection::Threshold threshold = detection::createThreshold(thresholdSigma, "pixel_stdev", positive);
    threshold.setIncludeMultiplier(1.0);
    auto fpSet = std::make_unique<detection::FootprintSet>(middle, threshold, positive ? "DETECTED" : "DETECTED_NEGATIVE",
                                                              npixMin);
    fpSet->setRegion(region);
    return fpSet;
}

/// Everything runDetection() hands back to a caller: the sigma actually
/// used, the original (mask-cleared) MaskedImage, the final detection image
/// (convolved + locally-background-subtracted, for significance lookups),
/// and the final grown FootprintSets per requested polarity.
struct DetectionOutput {
    double sigma;
    MaskedImage maskedImage;  // shallow copy of the exposure's own pixels (mask cleared of stale DETECTED* bits)
    MaskedImage middle;       // convolved [+ locally-bg-subtracted] detection image; origin may differ from maskedImage's
    std::unique_ptr<detection::FootprintSet> finalPositive;  // null if thresholdPolarity == "negative"
    std::unique_ptr<detection::FootprintSet> finalNegative;  // null if thresholdPolarity == "positive"
    // (footprints, polarity label, significance sign) for whichever
    // polarities were requested, in a stable order -- built from
    // finalPositive/finalNegative above, kept alongside them for
    // convenience so callers don't have to re-derive it.
    std::vector<std::tuple<detection::FootprintSet const*, char const*, double>> polarityResults;
};

/// Run the full detection pipeline (clearMask -> measure Psf sigma ->
/// convolve/removeBadPixels -> threshold per polarity -> temp local
/// background -> updatePeaks -> grow/finalize), identical to what
/// SourceDetectionTask.detectFootprints() does for detect()'s config.
/// `exposure` is modified in place (its mask's stale DETECTED* bits are
/// cleared, matching clearMask()); everything else is returned.
inline DetectionOutput runDetection(Exposure& exposure, Options const& opts) {
    MaskedImage maskedImage = exposure.getMaskedImage();  // shallow copy (afw convention): shares pixel data with exposure

    // Port of SourceDetectionTask.clearMask(), and *always* run (detectFootprints's
    // own clearMask=True default) -- see detect_min.cc's main() for the full
    // rationale (a real bug this caught, found by bisecting a calexp mismatch).
    {
        auto mask = maskedImage.getMask();
        auto bitmask = mask->getPlaneBitMask("DETECTED") | mask->getPlaneBitMask("DETECTED_NEGATIVE");
        *mask &= ~bitmask;
    }

    // --- sigma: measure from the exposure's own (real, deserialized) Psf,
    // exactly as SourceDetectionTask.getPsf() does, unless overridden. ---
    double sigma = opts.psfSigmaOverride;
    if (sigma < 0) {
        auto psf = exposure.getPsf();
        if (!psf) {
            throw lsst::pex::exceptions::RuntimeError("exposure has no Psf and --psf-sigma not given");
        }
        sigma = psf->computeShape(psf->getAveragePosition()).getDeterminantRadius();
    }

    // --- convolve (doSmooth) + remove bad pixels ---
    MaskedImage middle = convolveImage(maskedImage, sigma, opts.nSigmaForKernel, opts.doSmooth);
    removeBadPixels(middle, opts.excludeMaskPlanes);
    if (!opts.middleRawPath.empty()) {
        middle.getImage()->writeFits(opts.middleRawPath);
    }

    // --- threshold + detect, whichever polarity/ies were asked for ---
    bool wantPositive = opts.thresholdPolarity != "negative";
    bool wantNegative = opts.thresholdPolarity != "positive";
    std::unique_ptr<detection::FootprintSet> positive, negative;
    if (wantPositive) positive = detectPolarity(middle, maskedImage.getBBox(), opts.thresholdSigma, opts.npixMin, true);
    if (wantNegative) negative = detectPolarity(middle, maskedImage.getBBox(), opts.thresholdSigma, opts.npixMin, false);

    // --- temporary local background subtraction (once, shared by both
    // polarities -- matches applyTempLocalBackground, which subtracts from
    // `middle` a single time and only *then* re-detects peaks per polarity)
    // + peak re-detection ---
    if (opts.doTempLocalBackground) {
        math::StatisticsControl sctrl;
        auto badMask = image::Mask<image::MaskPixel>::getPlaneBitMask(
            {"BAD", "EDGE", "DETECTED", "DETECTED_NEGATIVE", "NO_DATA"});
        sctrl.setAndMask(badMask);
        sctrl.setNanSafe(false);
        int nx = maskedImage.getWidth() / opts.localBackgroundBinSize + 1;
        int ny = maskedImage.getHeight() / opts.localBackgroundBinSize + 1;
        math::BackgroundControl bctrl("AKIMA_SPLINE", nx, ny, "REDUCE_INTERP_ORDER", sctrl, "MEANCLIP");
        auto bg = math::makeBackground(maskedImage, bctrl);
        auto bgImage = bg->getImage<Pixel>("AKIMA_SPLINE", "REDUCE_INTERP_ORDER");
        if (!opts.backgroundPath.empty()) {
            bgImage->writeFits(opts.backgroundPath);
        }
        image::Image<Pixel> bgCropped(*bgImage, middle.getBBox(), image::PARENT, false);
        middle -= bgCropped;

        if (!opts.middlePath.empty()) {
            middle.getImage()->writeFits(opts.middlePath);
        }

        if (wantPositive) {
            detection::Threshold threshold2 = detection::createThreshold(opts.thresholdSigma, "pixel_stdev", true);
            threshold2.setIncludeMultiplier(1.0);
            updatePeaks(*positive, middle, threshold2, opts.npixMin);
        }
        if (wantNegative) {
            detection::Threshold threshold2 = detection::createThreshold(opts.thresholdSigma, "pixel_stdev", false);
            threshold2.setIncludeMultiplier(1.0);
            updatePeaks(*negative, middle, threshold2, opts.npixMin);
        }
    }

    // --- finalize: grow footprints (combinedGrow=True path only -- matches
    // SourceDetectionConfig's own combinedGrow=True default), per polarity ---
    auto finalize = [&](std::unique_ptr<detection::FootprintSet>& fpSet,
                         char const* maskPlane) -> std::unique_ptr<detection::FootprintSet> {
        if (!fpSet) return nullptr;
        std::unique_ptr<detection::FootprintSet> result;
        if (opts.nSigmaToGrow > 0) {
            int nGrow = static_cast<int>(opts.nSigmaToGrow * sigma + 0.5);
            result = std::make_unique<detection::FootprintSet>(*fpSet, nGrow, opts.isotropicGrow);
        } else {
            result = std::make_unique<detection::FootprintSet>(*fpSet);
        }
        result->setMask(maskedImage.getMask(), maskPlane);
        return result;
    };
    std::unique_ptr<detection::FootprintSet> finalPositive = finalize(positive, "DETECTED");
    std::unique_ptr<detection::FootprintSet> finalNegative = finalize(negative, "DETECTED_NEGATIVE");

    // (footprints, polarity label, significance sign) for whichever
    // polarities were requested -- significance is reported as a positive
    // "how many sigma" magnitude regardless of polarity (peakValue itself is
    // already negative for a negative-polarity peak, so the sign flips back
    // to positive when multiplied by polaritySign=-1; validated to match
    // the real Task's own significance field -- see ../compare_detection.py).
    std::vector<std::tuple<detection::FootprintSet const*, char const*, double>> polarityResults;
    if (finalPositive) polarityResults.emplace_back(finalPositive.get(), "positive", 1.0);
    if (finalNegative) polarityResults.emplace_back(finalNegative.get(), "negative", -1.0);

    return DetectionOutput{sigma, std::move(maskedImage), std::move(middle), std::move(finalPositive),
                            std::move(finalNegative), std::move(polarityResults)};
}

/// Per-mask-plane pixel counts, port of detection_on_image.py's own
/// `masked_pixel_summary` (in its `detect()`): total pixel count, one count
/// per plane in `excludeMaskPlanes` (in that order), and the union count
/// (pixels flagged in *any* of those planes -- Python's `(masked > 0).sum()`
/// after accumulating a 0/1 hit array per plane). Computed against
/// `maskedImage`'s mask exactly as read (callers should pass the exposure's
/// mask before runDetection() mutates it, though in practice that only ever
/// touches DETECTED/DETECTED_NEGATIVE, never a plane in excludeMaskPlanes,
/// so before/after wouldn't actually differ for the planes counted here).
struct MaskCensus {
    long long total;
    std::vector<std::pair<std::string, long long>> perPlane;  // (planeName, count), same order as excludeMaskPlanes
    long long masked;  // union: pixels flagged in any of excludeMaskPlanes
};

inline MaskCensus computeMaskCensus(MaskedImage const& maskedImage, std::vector<std::string> const& excludeMaskPlanes) {
    auto maskArray = maskedImage.getMask()->getArray();
    auto shape = maskArray.getShape();
    MaskCensus census;
    census.total = static_cast<long long>(shape[0]) * static_cast<long long>(shape[1]);
    census.masked = 0;
    if (excludeMaskPlanes.empty()) return census;

    std::vector<image::MaskPixel> bits;
    bits.reserve(excludeMaskPlanes.size());
    for (auto const& name : excludeMaskPlanes) bits.push_back(image::Mask<image::MaskPixel>::getPlaneBitMask(name));
    image::MaskPixel combined = image::Mask<image::MaskPixel>::getPlaneBitMask(excludeMaskPlanes);

    std::vector<long long> counts(excludeMaskPlanes.size(), 0);
    long long maskedCount = 0;
    for (int y = 0; y < shape[0]; ++y) {
        for (int x = 0; x < shape[1]; ++x) {
            auto v = maskArray[y][x];
            for (std::size_t i = 0; i < bits.size(); ++i) {
                if (v & bits[i]) ++counts[i];
            }
            if (v & combined) ++maskedCount;
        }
    }
    for (std::size_t i = 0; i < excludeMaskPlanes.size(); ++i) {
        census.perPlane.emplace_back(excludeMaskPlanes[i], counts[i]);
    }
    census.masked = maskedCount;
    return census;
}

/// Everything detection_on_image.py's detect() stashes in `peaks.meta`
/// (plus IMAGE_PATH, stashed by its main() instead) -- exposure-level facts
/// that apply to the whole detection run, not any one peak, so they belong
/// in a separate metadata sidecar rather than a CSV column. Any exposure
/// missing VisitInfo/Detector just omits that one field (rather than
/// erroring the whole run) since those are metadata conveniences, unlike
/// Wcs/PhotoCalib/Psf, which detect_and_measure can't function without.
struct Metadata {
    bool hasMjd = false;
    double mjd = 0.0;
    bool hasVisit = false;
    std::int64_t visit = 0;
    bool hasDetector = false;
    int detector = 0;
    double threshold;
    MaskCensus maskCensus;
    std::string imagePath;
};

inline Metadata computeMetadata(Exposure const& exposure, Options const& opts) {
    Metadata m;
    m.threshold = opts.thresholdSigma;
    m.maskCensus = computeMaskCensus(exposure.getMaskedImage(), opts.excludeMaskPlanes);

    auto visitInfo = exposure.getInfo()->getVisitInfo();
    if (visitInfo) {
        m.hasVisit = true;
        m.visit = visitInfo->getId();

        // Port of detection_on_image.py's own MJD computation: the
        // exposure's VisitInfo date as JD/UTC (its own `.get(JD, UTC)`
        // call -- see that file's long comment on why UTC is requested
        // explicitly rather than accepting DateTime::get's TAI default),
        // plus the same +0.5s DECam shutter-open latency correction, then
        // JD -> MJD is just -2400000.5 (exact, no scale change -- what
        // astropy.time.Time(..., format='jd').mjd does internally).
        auto date = visitInfo->getDate();
        double jdUtc = date.get(lsst::daf::base::DateTime::JD, lsst::daf::base::DateTime::UTC);
        double shutterHalfSecond = 0.5 / (24.0 * 60.0 * 60.0);
        m.mjd = jdUtc + shutterHalfSecond - 2400000.5;
        m.hasMjd = true;
    }

    auto detector = exposure.getDetector();
    if (detector) {
        m.hasDetector = true;
        m.detector = detector->getId();
    }

    try {
        m.imagePath = std::filesystem::canonical(opts.path).string();
    } catch (std::filesystem::filesystem_error const&) {
        m.imagePath = opts.path;  // fall back to whatever was given rather than fail the whole run over this
    }
    return m;
}

/// Formats a double at `precision` significant digits, then appends ".0"
/// if the result doesn't already look like a float (no '.'/'e') -- e.g.
/// 5.0 -> "5.0", not "5" (which detect_csv_to_table.py's int-or-float
/// coercion would read back as a Python *int*, unlike Python's own
/// argparse(type=float) --threshold, always a float) or "5.00000000000000"
/// (correct but needlessly noisy -- plain `showpoint` forces trailing
/// zeros out to the full precision even for a clean value like 5.0).
inline std::string formatMetaDouble(double v, int precision) {
    std::ostringstream oss;
    oss << std::setprecision(precision) << v;
    std::string s = oss.str();
    if (s.find_first_of(".eE") == std::string::npos) s += ".0";
    return s;
}

/// Writes `KEY=VALUE` lines, one per line, in the same key names/order
/// detection_on_image.py's own peaks.meta ends up with (MJD, VISIT,
/// DETECTOR, THRESHOLD, MASKED_TOTAL, MASKED_<plane>... , MASKED_MASKED,
/// IMAGE_PATH) -- meant to be read by ../detect_csv_to_table.py's
/// `--meta-file` and merged into the output table's .meta in this same
/// order, so the resulting ecsv's `meta: !!omap` block matches
/// detection_on_image.py's own output structurally.
inline void writeMetadata(std::ostream& out, Metadata const& m) {
    if (m.hasMjd) out << "MJD=" << formatMetaDouble(m.mjd, 17) << "\n";  // 17 sig figs: a double's round-trip precision
    if (m.hasVisit) out << "VISIT=" << m.visit << "\n";
    if (m.hasDetector) out << "DETECTOR=" << m.detector << "\n";
    out << "THRESHOLD=" << formatMetaDouble(m.threshold, 15) << "\n";
    out << "MASKED_TOTAL=" << m.maskCensus.total << "\n";
    for (auto const& [name, count] : m.maskCensus.perPlane) {
        std::string upper = name;
        std::transform(upper.begin(), upper.end(), upper.begin(),
                        [](unsigned char c) { return std::toupper(c); });
        out << "MASKED_" << upper << "=" << count << "\n";
    }
    out << "MASKED_MASKED=" << m.maskCensus.masked << "\n";
    out << "IMAGE_PATH=" << m.imagePath << "\n";
}

}  // namespace detect_common
