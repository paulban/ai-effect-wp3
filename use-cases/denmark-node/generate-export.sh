#!/bin/bash
# Danish Node — generate the AI-EFFECT onboarding package (blueprint.json,
# dockerinfo.json, generation_metadata.json, microservice/*.proto) plus a
# portal-ready ZIP.
#
# Point WP3_REPO inthe clone of https://github.com/AI-EFFECT/ai-effect-wp3
set -e

cd "$(dirname "$0")"

WP3_REPO="${WP3_REPO:-../..}"
GENERATOR="$WP3_REPO/scripts/onboarding-export-generator.py"

if [ ! -f "$GENERATOR" ]; then
    echo "Generator not found at $GENERATOR" >&2
    echo "Set WP3_REPO to your ai-effect-wp3 checkout, e.g.:" >&2
    echo "  WP3_REPO=/path/to/ai-effect-wp3 ./generate-export.sh" >&2
    exit 1
fi

# NOTE: pass an explicit directory name, not "." — the generator derives the
# pipeline name and image tags from the last path component, and Path(".").name
# is empty, which yields blueprint name "" and images "-<service>:latest".
python3 "$GENERATOR" "$(pwd)" export --overwrite --zip

echo ""
echo "Upload export.zip to the portal: Solutions -> Import ZIP"
