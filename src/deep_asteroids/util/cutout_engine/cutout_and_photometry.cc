// cutout_and_photometry.cc -- standalone (post-build) LSST-runtime-free
// binary that, given one exposure and a list of sky positions, emits
// per-position PSF-weighted forced photometry (the exact same algorithm as
// detect_and_measure.cc's computeForcedPhotometry / photometry.py's
// logL_position in deep_asteroids, extended to sub-pixel positions and a
// mask/zero_point/asymmetric-mag-error output -- see computeForcedPhotometryAt
// below) and, optionally, a lightweight image/mask/variance pixel cutout per
// position (matching deep_asteroids' cutout_service.py's make_cutout, but
// via afw's own MaskedImage::writeFits() instead of hand-assembling FITS
// HDUs -- that's exactly the IMAGE/MASK/VARIANCE HDU layout a calexp/
// differenceExp itself already uses, so no manual FITS work is needed).
//
// Unlike detect_and_measure/detect_min (which run detection footprint-by-
// footprint over the whole image), the positions here are supplied by the
// caller -- typically a crossmatch's predicted ephemeris position or a
// candidate tracklet's own detected point, not anything this binary detects
// itself. This is the "cutout + photometry" half of the persistent Python
// cutout_service.py in deep_asteroids, ported to the same
// build-once-with-opt_lsst / run-with-no-LSST-dependency pattern already
// used for detection -- see ../../../../DESIGN.md's crossmatch/cutout plan.
#include <cmath>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <limits>
#include <sstream>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "detect_common.h"

#include "lsst/afw/geom/SkyWcs.h"
#include "lsst/afw/image/PhotoCalib.h"
#include "lsst/geom/Point.h"
#include "lsst/geom/SpherePoint.h"

namespace dc = detect_common;

namespace {

struct Position {
    std::string id;
    bool isPixel;  // true: (a,b) is a pixel (x,y); false: (a,b) is a sky position (ra_deg,dec_deg)
    double a;
    double b;
};

struct ForcedPhotometryResult {
    bool inBounds = false;
    double x = 0.0;
    double y = 0.0;
    double flux = std::numeric_limits<double>::quiet_NaN();
    double fluxSigma = std::numeric_limits<double>::quiet_NaN();
    double snr = std::numeric_limits<double>::quiet_NaN();
    double logLikelihood = std::numeric_limits<double>::quiet_NaN();
    double mag = std::numeric_limits<double>::quiet_NaN();
    double magErrLo = std::numeric_limits<double>::quiet_NaN();
    double magErrHi = std::numeric_limits<double>::quiet_NaN();
    std::int64_t mask = 0;
    double zeroPoint = std::numeric_limits<double>::quiet_NaN();
};

/// Port of deep_asteroids' photometry.py:logL_position(), shift=(0,0) case
/// only (see detect_and_measure.cc's own copy of this note), generalized to
/// a sub-pixel `position` (unlike detect_and_measure's own
/// computeForcedPhotometry, which is only ever called at a detected peak's
/// *integer* position for parity with that binary's own production output)
/// and extended with logL_position's own mask/zero_point/asymmetric-mag-
/// error outputs, none of which detect_and_measure needed.
ForcedPhotometryResult computeForcedPhotometryAt(dc::Exposure const& exposure, lsst::geom::Point2D const& position) {
    ForcedPhotometryResult r;
    r.x = position.getX();
    r.y = position.getY();

    // skyToPixel() on a position far outside the Wcs's valid domain (e.g. a
    // bad crossmatch/ephemeris candidate) can come back NaN rather than
    // throwing -- std::lround(NaN) is undefined behavior, so this must be
    // caught here rather than falling through to it.
    if (std::isnan(position.getX()) || std::isnan(position.getY())) {
        return r;  // inBounds stays false
    }

    lsst::geom::Point2I intPos(static_cast<int>(std::lround(position.getX())),
                                static_cast<int>(std::lround(position.getY())));
    if (!exposure.getBBox().contains(intPos)) {
        return r;  // inBounds stays false
    }

    auto psf = exposure.getPsf();
    auto photoCalib = exposure.getPhotoCalib();

    lsst::geom::Box2I psfBBox = psf->computeImageBBox(position);
    psfBBox.clip(exposure.getBBox());
    if (psfBBox.getArea() == 0) {
        return r;
    }

    auto psfImage = psf->computeImage(position);  // shared_ptr<Image<double>>, sums to unity, unclipped bbox
    dc::image::Image<double> model(*psfImage, psfBBox, dc::image::PARENT, true);

    dc::image::Image<dc::Pixel> stampF(*exposure.getMaskedImage().getImage(), psfBBox, dc::image::PARENT, false);
    dc::image::Image<double> stamp(stampF, true);

    dc::image::Image<lsst::afw::image::VariancePixel> varianceF(*exposure.getMaskedImage().getVariance(), psfBBox,
                                                                   dc::image::PARENT, false);
    dc::image::Image<double> variance(varianceF, true);

    dc::image::Mask<lsst::afw::image::MaskPixel> maskF(*exposure.getMaskedImage().getMask(), psfBBox,
                                                        dc::image::PARENT, false);

    auto modelArr = model.getArray();
    auto stampArr = stamp.getArray();
    auto varianceArr = variance.getArray();
    auto maskArr = maskF.getArray();
    auto shape = modelArr.getShape();

    // c = Psi (signal), a = Phi (noise) -- Whidden et al. 2019 Eq. 19/20.
    double c = 0.0, a = 0.0;
    std::int64_t maskUnion = 0;
    for (int y = 0; y < shape[0]; ++y) {
        for (int x = 0; x < shape[1]; ++x) {
            double w = 1.0 / varianceArr[y][x];
            double m = modelArr[y][x];
            double s = stampArr[y][x];
            c += m * s * w;
            a += m * m * w;
            maskUnion |= static_cast<std::int64_t>(maskArr[y][x]);
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

    // mag_err_lo/hi follow logL_position's own convention exactly: mag_hi
    // (from flux+sigma) is the *smaller* magnitude, mag_lo (from
    // flux-sigma) the *larger* one, so mag_err_lo = mag - mag_hi and
    // mag_err_hi = mag_lo - mag are both >= 0 for a positive flux.
    double zp = photoCalib->instFluxToMagnitude(1.0);
    double mag = photoCalib->instFluxToMagnitude(flux);
    double magHi = photoCalib->instFluxToMagnitude(flux + fluxSigma);
    double magLo = photoCalib->instFluxToMagnitude(flux - fluxSigma);

    r.inBounds = true;
    r.flux = flux;
    r.fluxSigma = fluxSigma;
    r.snr = snr;
    r.logLikelihood = logL;
    r.mag = mag;
    r.magErrLo = mag - magHi;
    r.magErrHi = magLo - mag;
    r.mask = maskUnion;
    r.zeroPoint = zp;
    return r;
}

struct Options {
    std::string exposurePath;
    std::string positionsPath;   // '-' for stdin; empty if --sky-position/--pixel-position used instead
    bool hasSkyPosition = false;
    double skyRa = 0.0, skyDec = 0.0;
    bool hasPixelPosition = false;
    double pixelX = 0.0, pixelY = 0.0;
    std::string singleId = "0";  // --id, only meaningful with --sky-position/--pixel-position
    std::string csvPath = "-";   // '-' for stdout
    std::string cutoutDir;       // empty -> no cutouts written
    int cutoutSize = 51;
    bool quiet = false;
};

void usage(char const* prog) {
    std::cerr
        << "usage: " << prog << " <exposure.fits> (--positions <path|-> | --sky-position ra,dec |\n"
        << "                     --pixel-position ix,iy) [options]\n"
        << "  --positions path      positions to measure, one per line: id,ra_deg,dec_deg\n"
        << "                        (no header row; '-' reads from stdin)\n"
        << "  --sky-position ra,dec measure a single position given as a sky coordinate (degrees) --\n"
        << "                        resolved to a pixel position via the exposure's own Wcs\n"
        << "                        (skyToPixel()), same as one --positions row\n"
        << "  --pixel-position ix,iy measure a single position given directly as a pixel position --\n"
        << "                        no Wcs inversion needed, so this is the exact-parity way to\n"
        << "                        re-measure e.g. one of detect_and_measure's own detected peaks\n"
        << "                        (see README.md's \"Note on precision\")\n"
        << "  --id VALUE             id for the single position given via --sky-position/\n"
        << "                        --pixel-position (default: '0'); ignored with --positions\n"
        << "  --csv path             write photometry CSV here ('-' for stdout, default: -)\n"
        << "  --cutout-dir DIR       if given, also write <DIR>/<id>.fits (IMAGE/MASK/VARIANCE\n"
        << "                        HDUs, same layout as a calexp/differenceExp) per in-bounds position\n"
        << "  --cutout-size N        cutout side length in pixels, clipped to the exposure's own\n"
        << "                        bounds (default: 51; only used with --cutout-dir)\n"
        << "  --quiet                suppress the human-readable summary on stdout\n";
}

/// Splits "a,b" into two doubles, erroring out (not returning something
/// partial) if the format doesn't match -- shared by --sky-position and
/// --pixel-position, which both take exactly two comma-separated numbers.
std::pair<double, double> parsePair(std::string const& arg, std::string const& flagName, char const* prog) {
    std::vector<std::string> parts = dc::splitCsv(arg);
    if (parts.size() != 2) {
        std::cerr << "error: " << flagName << " wants exactly two comma-separated numbers, got: " << arg << "\n";
        usage(prog);
        std::exit(2);
    }
    return {std::stod(parts[0]), std::stod(parts[1])};
}

Options parseArgs(int argc, char** argv) {
    if (argc < 2) {
        usage(argv[0]);
        std::exit(2);
    }
    Options opts;
    opts.exposurePath = argv[1];
    for (int i = 2; i < argc; ++i) {
        std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                usage(argv[0]);
                std::exit(2);
            }
            return argv[++i];
        };
        if (arg == "--positions") opts.positionsPath = next();
        else if (arg == "--sky-position") {
            std::tie(opts.skyRa, opts.skyDec) = parsePair(next(), "--sky-position", argv[0]);
            opts.hasSkyPosition = true;
        }
        else if (arg == "--pixel-position") {
            std::tie(opts.pixelX, opts.pixelY) = parsePair(next(), "--pixel-position", argv[0]);
            opts.hasPixelPosition = true;
        }
        else if (arg == "--id") opts.singleId = next();
        else if (arg == "--csv") opts.csvPath = next();
        else if (arg == "--cutout-dir") opts.cutoutDir = next();
        else if (arg == "--cutout-size") opts.cutoutSize = std::stoi(next());
        else if (arg == "--quiet") opts.quiet = true;
        else {
            usage(argv[0]);
            std::exit(2);
        }
    }
    int nModes = (!opts.positionsPath.empty() ? 1 : 0) + (opts.hasSkyPosition ? 1 : 0)
                 + (opts.hasPixelPosition ? 1 : 0);
    if (nModes != 1) {
        std::cerr << "error: give exactly one of --positions, --sky-position, --pixel-position\n";
        usage(argv[0]);
        std::exit(2);
    }
    if (opts.positionsPath == "-" && opts.csvPath == "-") {
        std::cerr << "error: --positions - and --csv - can't both use stdin/stdout -- give --csv a real path\n";
        std::exit(2);
    }
    if (opts.cutoutSize <= 0) {
        std::cerr << "error: --cutout-size must be positive\n";
        std::exit(2);
    }
    return opts;
}

std::vector<Position> readPositions(std::string const& path) {
    std::ifstream file;
    if (path != "-") {
        file.open(path);
        if (!file) {
            std::cerr << "error: could not open --positions file " << path << "\n";
            std::exit(1);
        }
    }
    std::istream& in = (path == "-") ? std::cin : file;

    std::vector<Position> positions;
    std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        std::stringstream ss(line);
        std::string idField, raField, decField;
        if (!std::getline(ss, idField, ',') || !std::getline(ss, raField, ',') || !std::getline(ss, decField, ',')) {
            std::cerr << "error: malformed positions line (want id,ra_deg,dec_deg): " << line << "\n";
            std::exit(2);
        }
        positions.push_back(Position{idField, false, std::stod(raField), std::stod(decField)});
    }
    return positions;
}

/// Writes a lightweight cutout (no forced-photometry dependency) around
/// `pixelPos`, clipped to the exposure's own bbox -- same windowed-crop
/// convention as make_cutout() in deep_asteroids' cutout_service.py.
/// Returns false (writes nothing) if the clipped box is empty.
bool writeCutout(dc::Exposure const& exposure, lsst::geom::Point2D const& pixelPos, int size,
                  std::string const& outputPath) {
    if (std::isnan(pixelPos.getX()) || std::isnan(pixelPos.getY())) return false;
    int x = static_cast<int>(std::lround(pixelPos.getX()));
    int y = static_cast<int>(std::lround(pixelPos.getY()));
    int half = size / 2;
    lsst::geom::Box2I cutoutBBox(lsst::geom::Point2I(x - half, y - half), lsst::geom::Extent2I(size, size));
    cutoutBBox.clip(exposure.getBBox());
    if (cutoutBBox.getArea() == 0) return false;

    dc::MaskedImage cutout(exposure.getMaskedImage(), cutoutBBox, dc::image::PARENT, true);  // deep copy
    cutout.writeFits(outputPath);
    return true;
}

}  // namespace

int main(int argc, char** argv) {
    Options opts = parseArgs(argc, argv);
    std::vector<Position> positions;
    if (!opts.positionsPath.empty()) {
        positions = readPositions(opts.positionsPath);
    } else if (opts.hasSkyPosition) {
        positions.push_back(Position{opts.singleId, false, opts.skyRa, opts.skyDec});
    } else {
        positions.push_back(Position{opts.singleId, true, opts.pixelX, opts.pixelY});
    }

    // See detect_and_measure.cc's own copy of this comment: piping the CSV
    // to stdout needs LSST's own stdout-bound logging suppressed for the
    // read+measure window, or it corrupts the stream.
    std::unique_ptr<dc::StdoutSuppressor> suppressor;
    if (opts.csvPath == "-") suppressor = std::make_unique<dc::StdoutSuppressor>();

    auto exposure = dc::Exposure::readFits(opts.exposurePath);
    auto wcs = exposure.getWcs();
    if (!wcs) {
        suppressor.reset();
        std::cerr << "error: exposure has no Wcs; can't convert ra/dec to pixel positions\n";
        return 1;
    }
    if (!exposure.getPhotoCalib()) {
        suppressor.reset();
        std::cerr << "error: exposure has no PhotoCalib; can't convert flux to mag\n";
        return 1;
    }
    if (!exposure.getPsf()) {
        suppressor.reset();
        std::cerr << "error: exposure has no Psf; can't run forced photometry\n";
        return 1;
    }
    // LSST's own stdout-bound logging only happens during readFits() above
    // (e.g. ExposureFitsReader's SUMMARY_STATS WARN) -- restore real stdout
    // now, before any CSV writing, rather than leaving it suppressed
    // through the whole measurement loop below (which would otherwise
    // silently swallow `--csv -`'s entire output).
    suppressor.reset();

    if (!opts.cutoutDir.empty()) {
        std::filesystem::create_directories(opts.cutoutDir);
    }

    std::ofstream csvFile;
    if (opts.csvPath != "-") csvFile.open(opts.csvPath);
    std::ostream& csvOut = (opts.csvPath == "-") ? std::cout : csvFile;
    csvOut << "id,in_bounds,ra_deg,dec_deg,x,y,flux,flux_sigma,snr,log_likelihood,mag,mag_err_lo,mag_err_hi,mask,"
              "zero_point,cutout_written\n";
    // 10 decimal places (~1 microarcsec) for ra_deg/dec_deg -- 6 (this
    // stream's previous precision) is coarser than skyToPixel()'s own
    // solver tolerance at DECam's plate scale and was confirmed (see
    // README.md's "Note on precision") to reintroduce a ~0.3% flux/~0.003
    // mag discrepancy purely from CSV rounding if this output were ever
    // fed back in as another call's --positions file.
    csvOut << std::fixed << std::setprecision(10);

    std::size_t nInBounds = 0, nCutouts = 0;
    for (auto const& pos : positions) {
        lsst::geom::Point2D pixelPos;
        lsst::geom::SpherePoint sky;
        if (pos.isPixel) {
            pixelPos = lsst::geom::Point2D(pos.a, pos.b);
            sky = wcs->pixelToSky(pixelPos);  // analytic forward transform -- for CSV output only
        } else {
            sky = lsst::geom::SpherePoint(pos.a * lsst::geom::degrees, pos.b * lsst::geom::degrees);
            pixelPos = wcs->skyToPixel(sky);  // iterative inverse -- see README.md's precision note
        }
        ForcedPhotometryResult phot = computeForcedPhotometryAt(exposure, pixelPos);

        bool cutoutWritten = false;
        if (phot.inBounds && !opts.cutoutDir.empty()) {
            std::string outputPath = opts.cutoutDir + "/" + pos.id + ".fits";
            cutoutWritten = writeCutout(exposure, pixelPos, opts.cutoutSize, outputPath);
        }
        if (phot.inBounds) ++nInBounds;
        if (cutoutWritten) ++nCutouts;

        csvOut << pos.id << "," << (phot.inBounds ? "1" : "0") << ","
               << sky.getRa().asDegrees() << "," << sky.getDec().asDegrees() << ","
               << phot.x << "," << phot.y << "," << phot.flux << "," << phot.fluxSigma << "," << phot.snr << ","
               << phot.logLikelihood << "," << phot.mag << "," << phot.magErrLo << "," << phot.magErrHi << ","
               << phot.mask << "," << phot.zeroPoint << "," << (cutoutWritten ? "1" : "0") << "\n";
    }

    if (!opts.quiet) {
        std::cout << "measured " << positions.size() << " position(s): " << nInBounds << " in-bounds";
        if (!opts.cutoutDir.empty()) std::cout << ", " << nCutouts << " cutout(s) written to " << opts.cutoutDir;
        std::cout << "\n";
    }
    return 0;
}
