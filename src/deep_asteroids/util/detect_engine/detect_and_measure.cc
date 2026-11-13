// detect_and_measure.cc -- detect_min.cc plus two extra conversions, using
// the exposure's own persisted models rather than any external one, exactly
// as detection_on_image.py's detect() does:
//   - RA/Dec: exposure.getWcs()->pixelToSky(Point2D(fx,fy)) (equivalent to
//     detection_on_image.py's `exposure.wcs.getTransform().applyForward(...)`).
//   - mag: a port of detection_on_image.py's logL_position() -- a
//     single-parameter, weighted-least-squares PSF flux fit at the peak's
//     *integer* (ix,iy) position (Whidden et al. 2019 Eq. 19/20/22:
//     psi/phi/alpha_ML), then that flux's magnitude via the exposure's own
//     PhotoCalib. This is NOT simply instFluxToMagnitude(value) on the raw
//     peak pixel value -- detect()'s own production output has no such
//     column; its `forced_mag` (what this file's `mag` reproduces) is
//     exactly this PSF-fit quantity, computed by forced_exposures() calling
//     logL_position(exposure, Point2D(i_x, i_y), shift=[0, 0]) for every
//     peak. Only the shift=[0,0] case is ported, since that's the only one
//     detect()'s own call to it ever uses (scipy.ndimage.shift with a zero
//     shift is a no-op) -- see computeForcedPhotometry() below.
//
// The detection algorithm itself is identical to detect_min's -- both
// binaries call detect_common::runDetection() so they run the exact same
// compiled logic. See detect_min.cc's own top comment and ../README.md for
// what that algorithm is and what's been validated. This file adds the
// CLI/output layer plus the forced-photometry step above.
//
// A negative-polarity peak's fitted flux can come out negative too, so its
// `mag` can come out NaN (log of a negative number) -- same as what
// PhotoCalib::instFluxToMagnitude does in Python on a negative flux, not a
// special case here.
#include <cmath>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <string>
#include <vector>

#include "detect_common.h"

#include "lsst/afw/geom/SkyWcs.h"
#include "lsst/afw/image/PhotoCalib.h"
#include "lsst/geom/Point.h"

namespace dc = detect_common;

namespace {

struct ForcedPhotometry {
    double flux;            // alpha_ML = c/a, i.e. psi/phi (Whidden et al. 2019 Eq. 22)
    double fluxSigma;       // 1/sqrt(a), the flux estimate's standard deviation
    double snr;             // c/sqrt(a) (Eq. 26) -- not flux/fluxSigma, matching logL_position's own comment on this
    double logLikelihood;   // -0.5 * sum(weights * (flux*model - stamp)^2)
    double mag;             // photoCalib->instFluxToMagnitude(flux)
};

/// Port of detection_on_image.py's logL_position(), restricted to the
/// shift=(0,0) case -- the only one detect()'s own forced_exposures() call
/// ever uses. A single-parameter (flux) weighted-least-squares fit of the
/// exposure's own Psf model (evaluated at `position`, normalized to unit
/// flux) against the exposure's own pixels, inverse-variance-weighted, over
/// the Psf's own image bbox at that position (clipped to the exposure's
/// bbox, same as `psfBBox.clip(eBBox)` there). `position` should be the
/// peak's *integer* (ix, iy) -- detect()'s own call passes
/// Point2D(i_x, i_y), not the float centroid, so callers here should match
/// that for parity (validated directly against forced_mag -- see
/// ../README.md).
ForcedPhotometry computeForcedPhotometry(dc::Exposure const& exposure, lsst::geom::Point2D const& position) {
    auto psf = exposure.getPsf();
    auto photoCalib = exposure.getPhotoCalib();

    lsst::geom::Box2I psfBBox = psf->computeImageBBox(position);
    psfBBox.clip(exposure.getBBox());

    auto psfImage = psf->computeImage(position);  // shared_ptr<Image<double>>, sums to unity, unclipped bbox
    dc::image::Image<double> model(*psfImage, psfBBox, dc::image::PARENT, true);

    dc::image::Image<dc::Pixel> stampF(*exposure.getMaskedImage().getImage(), psfBBox, dc::image::PARENT, false);
    dc::image::Image<double> stamp(stampF, true);

    dc::image::Image<lsst::afw::image::VariancePixel> varianceF(*exposure.getMaskedImage().getVariance(), psfBBox,
                                                                   dc::image::PARENT, false);
    dc::image::Image<double> variance(varianceF, true);

    auto modelArr = model.getArray();
    auto stampArr = stamp.getArray();
    auto varianceArr = variance.getArray();
    auto shape = modelArr.getShape();

    // c = Psi (signal), a = Phi (noise) -- Whidden et al. 2019 Eq. 19/20.
    double c = 0.0, a = 0.0;
    for (int y = 0; y < shape[0]; ++y) {
        for (int x = 0; x < shape[1]; ++x) {
            double w = 1.0 / varianceArr[y][x];
            double m = modelArr[y][x];
            double s = stampArr[y][x];
            c += m * s * w;
            a += m * m * w;
        }
    }
    double flux = c / a;
    double fluxSigma = 1.0 / std::sqrt(a);
    double snr = c / std::sqrt(a);

    double logL = 0.0;
    for (int y = 0; y < shape[0]; ++y) {
        for (int x = 0; x < shape[1]; ++x) {
            double w = 1.0 / varianceArr[y][x];
            double residual = flux * modelArr[y][x] - stampArr[y][x];
            logL += w * residual * residual;
        }
    }
    logL *= -0.5;

    double mag = photoCalib->instFluxToMagnitude(flux);

    return ForcedPhotometry{flux, fluxSigma, snr, logL, mag};
}

void usage(char const* prog) {
    std::cerr
        << "usage: " << prog << " <exposure.fits> [options]\n"
        << "  --sigma S                  detection threshold, pixel_stdev units (default: 5.0)\n"
        << "  --npix-min N               minimum footprint size in pixels (default: 1)\n"
        << "  --threshold-polarity P     positive, negative, or both (default: positive)\n"
        << "  --psf-sigma S              override the measured PSF sigma used for smoothing/growing\n"
        << "                             (forced photometry still needs the exposure's real Psf model)\n"
        << "  --no-smooth                skip PSF-matched-filter convolution (default: on)\n"
        << "  --no-temp-local-background skip temporary local background subtraction (default: on)\n"
        << "  --exclude-mask-planes P,P  comma-separated mask planes to zero before thresholding\n"
        << "                             (default: EDGE,SAT,SUSPECT,BAD,NO_DATA,STREAK,CROSSTALK,NOT_DEBLENDED)\n"
        << "  --csv path                 write every final peak as CSV ('-' for stdout)\n"
        << "                             columns: detect_min's plus ra_deg,dec_deg,flux,flux_sigma,\n"
        << "                             snr,log_likelihood,mag (the last five from forced PSF\n"
        << "                             photometry at the peak, i.e. detect()'s own forced_* / mag columns)\n"
        << "  --heavy path               write every final footprint's own pixel values as CSV\n"
        << "  --metadata path            write exposure-level KEY=VALUE metadata ('-' for stdout) --\n"
        << "                             MJD,VISIT,DETECTOR,THRESHOLD,MASKED_*,IMAGE_PATH, matching\n"
        << "                             detection_on_image.py's own peaks.meta; feed to\n"
        << "                             ../detect_csv_to_table.py's --meta-file\n"
        << "  --quiet                    suppress the human-readable summary/preview on stdout\n";
}

dc::Options parseArgs(int argc, char** argv) {
    if (argc < 2) {
        usage(argv[0]);
        std::exit(2);
    }
    dc::Options opts;
    opts.path = argv[1];
    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                usage(argv[0]);
                std::exit(2);
            }
            return argv[++i];
        };
        if (arg == "--sigma") opts.thresholdSigma = std::stod(next());
        else if (arg == "--npix-min") opts.npixMin = std::stoi(next());
        else if (arg == "--threshold-polarity") {
            opts.thresholdPolarity = next();
            if (opts.thresholdPolarity != "positive" && opts.thresholdPolarity != "negative"
                && opts.thresholdPolarity != "both") {
                std::cerr << "error: --threshold-polarity must be positive, negative, or both\n";
                std::exit(2);
            }
        }
        else if (arg == "--psf-sigma") opts.psfSigmaOverride = std::stod(next());
        else if (arg == "--no-smooth") opts.doSmooth = false;
        else if (arg == "--no-temp-local-background") opts.doTempLocalBackground = false;
        else if (arg == "--exclude-mask-planes") opts.excludeMaskPlanes = dc::splitCsv(next());
        else if (arg == "--csv") opts.csvPath = next();
        else if (arg == "--heavy") opts.heavyPath = next();
        else if (arg == "--metadata") opts.metadataPath = next();
        else if (arg == "--dump-background") opts.backgroundPath = next();
        else if (arg == "--dump-middle") opts.middlePath = next();
        else if (arg == "--dump-middle-raw") opts.middleRawPath = next();
        else if (arg == "--quiet") opts.quiet = true;
        else {
            usage(argv[0]);
            std::exit(2);
        }
    }
    if (opts.csvPath == "-" && opts.metadataPath == "-") {
        std::cerr << "error: --csv - and --metadata - can't both go to stdout -- give --metadata a real path "
                      "(e.g. --metadata meta.txt), since detect_csv_to_table.py --meta-file reads it separately "
                      "from the piped CSV\n";
        std::exit(2);
    }
    return opts;
}

}  // namespace

int main(int argc, char** argv) {
    dc::Options opts = parseArgs(argc, argv);

    // See detect_min.cc's own copy of this comment / StdoutSuppressor's doc
    // comment in detect_common.h: piping the peak CSV (or metadata) to
    // stdout (--csv -, --metadata -) needs LSST's own stdout-bound logging
    // suppressed for the read+detect+measure window, or it corrupts the
    // stream.
    std::unique_ptr<dc::StdoutSuppressor> suppressor;
    if (opts.csvPath == "-" || opts.metadataPath == "-") suppressor = std::make_unique<dc::StdoutSuppressor>();

    auto exposure = dc::Exposure::readFits(opts.path);
    if (!opts.quiet) {
        auto const& mi = exposure.getMaskedImage();
        std::cout << "read " << opts.path << " -- " << mi.getWidth() << "x" << mi.getHeight() << " pixels\n";
    }

    // Computed from the exposure as read, before runDetection() below
    // mutates its mask -- see detect_min.cc's own copy of this comment.
    dc::Metadata metadata = dc::computeMetadata(exposure, opts);

    auto wcs = exposure.getWcs();
    auto photoCalib = exposure.getPhotoCalib();
    auto psf = exposure.getPsf();
    if (!wcs) {
        suppressor.reset();
        std::cerr << "error: exposure has no Wcs; can't convert fx/fy to ra/dec\n";
        return 1;
    }
    if (!photoCalib) {
        suppressor.reset();
        std::cerr << "error: exposure has no PhotoCalib; can't convert flux to mag\n";
        return 1;
    }
    if (!psf) {
        suppressor.reset();
        std::cerr << "error: exposure has no Psf; can't run forced photometry (needed even with --psf-sigma,\n"
                      "       which only overrides the *detection* smoothing/growing sigma)\n";
        return 1;
    }

    dc::DetectionOutput out = [&] {
        try {
            return dc::runDetection(exposure, opts);
        } catch (lsst::pex::exceptions::Exception const& e) {
            suppressor.reset();
            std::cerr << "error: " << e.what() << "\n";
            std::exit(1);
        }
    }();
    suppressor.reset();  // restore real stdout before any of our own output below

    if (!opts.quiet) std::cout << "psf sigma: " << out.sigma << " px\n";

    std::size_t totalFootprints = 0, totalPeaks = 0;
    for (auto const& [fpSet, label, sign] : out.polarityResults) {
        totalFootprints += fpSet->getFootprints()->size();
        for (auto const& fp : *fpSet->getFootprints()) totalPeaks += fp->getPeaks().size();
    }

    if (!opts.quiet) {
        std::cout << "found " << totalFootprints << " footprint(s) at " << opts.thresholdSigma
                  << "-sigma, npixMin=" << opts.npixMin << ", polarity=" << opts.thresholdPolarity
                  << " (doSmooth=" << opts.doSmooth << " doTempLocalBackground=" << opts.doTempLocalBackground
                  << ")\n"
                  << totalPeaks << " peak(s) total\n";
    }

    // --- peak CSV: detect_min's columns plus ra_deg/dec_deg (from the
    // exposure's own Wcs, evaluated at the peak's float centroid fx/fy) and
    // flux/flux_sigma/snr/log_likelihood/mag (from forced PSF photometry at
    // the peak's integer ix/iy position, via the exposure's own Psf +
    // PhotoCalib -- see computeForcedPhotometry() above; this is what
    // detect()'s own forced_flux/forced_sigma/snr/log_likelihood/forced_mag
    // columns are, not a simpler raw-peak-value magnitude). ---
    if (!opts.csvPath.empty()) {
        std::ofstream csvFile;
        if (opts.csvPath != "-") csvFile.open(opts.csvPath);
        std::ostream& csvOut = (opts.csvPath == "-") ? std::cout : csvFile;
        csvOut << "polarity,footprint_id,footprint_area,ix,iy,fx,fy,value,significance,ra_deg,dec_deg,"
                  "flux,flux_sigma,snr,log_likelihood,mag\n";
        csvOut << std::fixed << std::setprecision(6);
        auto middleVariance = out.middle.getVariance()->getArray();
        auto middleOrigin = out.middle.getXY0();
        int footprintId = 0;
        for (auto const& [fpSet, label, sign] : out.polarityResults) {
            for (auto const& fp : *fpSet->getFootprints()) {
                for (auto const& peak : fp->getPeaks()) {
                    int mx = peak.getIx() - middleOrigin.getX();
                    int my = peak.getIy() - middleOrigin.getY();
                    double significance = std::nan("");
                    if (my >= 0 && my < middleVariance.getShape()[0] && mx >= 0 && mx < middleVariance.getShape()[1]) {
                        significance = sign * peak.getPeakValue() / std::sqrt(middleVariance[my][mx]);
                    }
                    lsst::geom::SpherePoint sky = wcs->pixelToSky(lsst::geom::Point2D(peak.getFx(), peak.getFy()));
                    // detect()'s own forced_exposures() call passes the
                    // peak's *integer* (i_x, i_y), not (fx, fy) -- matched
                    // here for parity.
                    ForcedPhotometry phot = computeForcedPhotometry(
                        exposure, lsst::geom::Point2D(peak.getIx(), peak.getIy()));
                    csvOut << label << "," << footprintId << "," << fp->getArea() << "," << peak.getIx() << ","
                           << peak.getIy() << "," << peak.getFx() << "," << peak.getFy() << "," << peak.getPeakValue()
                           << "," << significance << "," << sky.getRa().asDegrees() << "," << sky.getDec().asDegrees()
                           << "," << phot.flux << "," << phot.fluxSigma << "," << phot.snr << "," << phot.logLikelihood
                           << "," << phot.mag << "\n";
                }
                ++footprintId;
            }
        }
        if (!opts.quiet && opts.csvPath != "-") std::cout << "wrote " << totalPeaks << " peak(s) to " << opts.csvPath << "\n";
    }

    // --- exposure-level metadata: MJD/VISIT/DETECTOR/THRESHOLD/MASKED_*/
    // IMAGE_PATH, matching detection_on_image.py's own peaks.meta -- see
    // computeMetadata()/writeMetadata() in detect_common.h. ---
    if (!opts.metadataPath.empty()) {
        std::ofstream metaFile;
        if (opts.metadataPath != "-") metaFile.open(opts.metadataPath);
        std::ostream& metaOut = (opts.metadataPath == "-") ? std::cout : metaFile;
        dc::writeMetadata(metaOut, metadata);
        if (!opts.quiet && opts.metadataPath != "-") std::cout << "wrote metadata to " << opts.metadataPath << "\n";
    }

    // --- heavy footprints: identical to detect_min's -- ra/dec and the
    // forced-photometry columns are per-peak quantities, not per-pixel, so
    // there's nothing to add here. ---
    if (!opts.heavyPath.empty()) {
        std::ofstream out2(opts.heavyPath);
        out2 << "polarity,footprint_id,x,y,image,mask,variance\n";
        out2 << std::fixed << std::setprecision(6);
        int footprintId = 0;
        std::size_t totalPixels = 0;
        for (auto const& [fpSet, label, sign] : out.polarityResults) {
            for (auto const& fp : *fpSet->getFootprints()) {
                dc::detection::HeavyFootprint<dc::Pixel> heavy(*fp, out.maskedImage);
                auto imageArray = heavy.getImageArray();
                auto maskArray = heavy.getMaskArray();
                auto varianceArray = heavy.getVarianceArray();
                std::size_t i = 0;
                for (auto const& span : *fp->getSpans()) {
                    int y = span.getY();
                    for (int x = span.getX0(); x <= span.getX1(); ++x, ++i) {
                        out2 << label << "," << footprintId << "," << x << "," << y << "," << imageArray[i] << ","
                             << maskArray[i] << "," << varianceArray[i] << "\n";
                    }
                }
                totalPixels += i;
                ++footprintId;
            }
        }
        if (!opts.quiet) std::cout << "wrote " << totalPixels << " pixel(s) across " << totalFootprints
                                    << " heavy footprint(s) to " << opts.heavyPath << "\n";
    }

    if (!opts.quiet) {
        std::cout << std::fixed << std::setprecision(2);
        std::size_t shown = 0;
        for (auto const& [fpSet, label, sign] : out.polarityResults) {
            for (auto const& fp : *fpSet->getFootprints()) {
                auto bbox = fp->getBBox();
                for (auto const& peak : fp->getPeaks()) {
                    if (shown >= 20) break;
                    lsst::geom::SpherePoint sky = wcs->pixelToSky(lsst::geom::Point2D(peak.getFx(), peak.getFy()));
                    ForcedPhotometry phot = computeForcedPhotometry(
                        exposure, lsst::geom::Point2D(peak.getIx(), peak.getIy()));
                    std::cout << "  [" << label << "] footprint area=" << fp->getArea() << " bbox=[" << bbox.getMinX()
                              << "," << bbox.getMinY() << " - " << bbox.getMaxX() << "," << bbox.getMaxY() << "]"
                              << "  peak=(" << peak.getIx() << "," << peak.getIy() << ") value=" << peak.getPeakValue()
                              << " ra=" << sky.getRa().asDegrees() << " dec=" << sky.getDec().asDegrees()
                              << " mag=" << phot.mag << "\n";
                    ++shown;
                }
                if (shown >= 20) break;
            }
            if (shown >= 20) break;
        }
        if (shown >= 20) {
            std::cout << "  ... (" << totalPeaks - shown << " more, truncated -- use --csv for the full list)\n";
        }
    }
    return 0;
}
