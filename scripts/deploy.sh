#!/usr/bin/env bash
# Deploy one explicitly approved public release to the gh-pages branch.
# Usage: bash scripts/deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
PUBLIC_MANIFEST="$REPO_ROOT/public/release.json"

if [ ! -f "$PUBLIC_MANIFEST" ]; then
    echo "Refusing to deploy: public/release.json is required and must be explicitly reviewed." >&2
    exit 2
fi

echo "Building explicit public release..."
DEPLOY_DIR=$(mktemp -d)
cleanup() {
    rm -rf "$DEPLOY_DIR"
}
trap cleanup EXIT

python3 "$REPO_ROOT/scripts/build_public_site.py" --output-dir "$DEPLOY_DIR"

echo "Deploying to gh-pages..."

# Push to gh-pages branch, using the repo-level git identity
DEPLOY_USER=$(cd "$REPO_ROOT" && git config user.name)
DEPLOY_EMAIL=$(cd "$REPO_ROOT" && git config user.email)

cd "$DEPLOY_DIR"
git init
git config user.name "$DEPLOY_USER"
git config user.email "$DEPLOY_EMAIL"
git checkout -b gh-pages
git add -A
git commit -m "Deploy $(date +%Y-%m-%d\ %H:%M)"
git remote add origin "$(cd "$REPO_ROOT" && git remote get-url origin)"
git push origin gh-pages --force

REPO_NAME=$(cd "$REPO_ROOT" && git remote get-url origin | sed 's/.*[:/]\([^/]*\)\.git/\1/' | sed 's/.*[:/]\([^/]*\)$/\1/')
REPO_OWNER=$(cd "$REPO_ROOT" && git remote get-url origin | sed 's/.*[:/]\([^/]*\)\/[^/]*/\1/')
echo "Deployed! Site: https://${REPO_OWNER}.github.io/${REPO_NAME}/"
