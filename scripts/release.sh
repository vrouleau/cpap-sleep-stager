#!/usr/bin/env bash
# Cut a release: bump the version, tag main, push the tag.
#
#   scripts/release.sh patch            # v0.1.0 -> v0.1.1  (fixes)
#   scripts/release.sh minor            # v0.1.1 -> v0.2.0  (new features)
#   scripts/release.sh major            # v0.2.0 -> v1.0.0  (breaking changes)
#   scripts/release.sh patch --dry-run  # show what would happen, change nothing
#   scripts/release.sh patch --yes      # don't ask for confirmation
#
# Pushing the tag starts the "Images" workflow on GitHub: it runs the tests, publishes
# ghcr.io/<repo>:X.Y.Z, :X.Y and :latest, then creates the GitHub
# Release with notes generated from the commits. This script only checks and tags.
set -euo pipefail

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "release: $*" >&2; exit 1; }

bump="" dry=0 yes=0
for a in "$@"; do
  case "$a" in
    major|minor|patch) bump="$a" ;;
    --dry-run|-n) dry=1 ;;
    --yes|-y) yes=1 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $a" >&2; usage 1 ;;
  esac
done
[ -n "$bump" ] || usage 1

cd "$(git rev-parse --show-toplevel)"
branch=$(git rev-parse --abbrev-ref HEAD)

# --- the release must be exactly what is on GitHub's main -------------------------
[ "$branch" = "main" ] || die "you are on '$branch'; releases are cut from main (git checkout main && git pull)"
[ -z "$(git status --porcelain)" ] || die "uncommitted changes; commit or stash them first"
git fetch --quiet --tags origin main
head=$(git rev-parse HEAD)
[ "$head" = "$(git rev-parse origin/main)" ] \
  || die "local main and origin/main differ; push or pull first (git status -sb)"

# --- next version from the latest vX.Y.Z tag ----------------------------------------
last=$(git tag -l 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n1)
last=${last:-v0.0.0}
IFS=. read -r major minor patch <<<"${last#v}"
case "$bump" in
  major) major=$((major + 1)); minor=0; patch=0 ;;
  minor) minor=$((minor + 1)); patch=0 ;;
  patch) patch=$((patch + 1)) ;;
esac
next="v$major.$minor.$patch"

if git rev-parse -q --verify "refs/tags/$next" >/dev/null; then die "$next already exists"; fi
if tagged=$(git tag --points-at HEAD 'v*' | head -n1) && [ -n "$tagged" ]; then
  die "this commit is already released as $tagged; nothing new to release"
fi
range="$last..HEAD"; [ "$last" = "v0.0.0" ] && range="HEAD"
changes=$(git log --oneline --no-merges "$range")
[ -n "$changes" ] || die "no commits since $last"

# --- CI must be green on this exact commit (skipped with a warning without gh) ------
ci="unknown"
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  ci=$(gh run list --workflow ci.yml --commit "$head" --limit 1 \
        --json status,conclusion \
        -q '.[0] | if . == null then "none" elif (.conclusion // "") == "" then .status else .conclusion end' \
        2>/dev/null || echo "unknown")
  case "$ci" in
    success) ;;
    none) die "no CI run for ${head:0:7} yet; wait for the push to main to be tested" ;;
    queued|in_progress|pending|waiting|requested)
      die "CI is still running on ${head:0:7}; try again when it is green (gh run watch)" ;;
    unknown) echo "release: warning: could not read the CI status; check the Actions tab" >&2 ;;
    *) die "CI on ${head:0:7} is '$ci'; fix it before releasing" ;;
  esac
else
  echo "release: warning: GitHub CLI (gh) not available; CI status not checked" >&2
fi

echo "Release $next  ($bump bump from $last)  on main @ ${head:0:7}  CI: $ci"
echo
echo "Changes since $last:"
echo "$changes" | sed 's/^/  /'
echo

if [ "$dry" = 1 ]; then echo "(dry run: nothing tagged or pushed)"; exit 0; fi
if [ "$yes" != 1 ]; then
  read -r -p "Tag and push $next? [y/N] " ok
  [[ "$ok" =~ ^[Yy]$ ]] || die "aborted"
fi

git tag -a "$next" -m "$next"
git push --quiet origin "$next"
repo=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo "<owner>/<repo>")
echo
echo "Pushed $next. GitHub now runs the tests, publishes the images and creates the release:"
echo "  progress: https://github.com/$repo/actions/workflows/images.yml"
echo "  release:  https://github.com/$repo/releases/tag/$next  (when the images are done)"
echo "  on the NAS: docker compose pull && docker compose up -d"
