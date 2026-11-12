#!/usr/bin/env bash
# Shared setup for the examples/*.sh scripts: a fresh-enough
# deep-asteroids database, deep-db reachable, and the Butler registry
# credentials -- then one function each script calls to ingest the
# (night, field) it needs before running `viz tracklet`.
#
# Run from a Slurm job (`salloc -c 2 --mem 8G`), not a login node -- this
# does real ingest work and spawns the cutout_and_photometry subprocess.
set -euo pipefail

: "${DEEP_ASTEROIDS_DB:=sqlite:///$(pwd)/deep_asteroids_examples.sqlite}"
export DEEP_ASTEROIDS_DB
echo "DEEP_ASTEROIDS_DB=$DEEP_ASTEROIDS_DB" >&2

: "${REPO_DIR:=/gscratch/dirac/DEEP/repo}"
: "${REGISTRY_HOST:=epyc.astro.washington.edu/deep_klone_2}"
: "${PGUSER:?Set PGUSER, e.g. 'source /gscratch/dirac/DEEP/data/credentials' first}"
: "${PGPASSWORD:?Set PGPASSWORD, e.g. 'source /gscratch/dirac/DEEP/data/credentials' first}"
REGISTRY_URI="postgresql://${PGUSER}:${PGPASSWORD}@${REGISTRY_HOST}"

# $DEEP_DB: use whatever the caller already set (a cache path or URL);
# otherwise fall back to deep-db's own cached copy -- fetched once,
# explicitly, never by this script (see the README's own `database
# fetch-deep-db` step). deep-asteroids itself raises a clear
# FileNotFoundError if neither is reachable, so nothing to check here.

deep-asteroids database create

# Ingests search definitions + the one known-tracklet source this
# example needs + Butler image links, for one (night, field) -- skips
# work already done (idempotent), so re-running a script is cheap.
setup_night() {
    local night="$1" field="$2" tracklet_source="$3"
    deep-asteroids ingest searches --night "$night"
    deep-asteroids ingest tracklets "$tracklet_source" --night "$night"
    deep-asteroids ingest images \
        --registry-uri "$REGISTRY_URI" \
        --repo-dir "$REPO_DIR" \
        --product-type differenceExp \
        --night "$night" --field "$field"
}
