#!/usr/bin/env bash
# Apply branch protection rules for main and next using the GitHub CLI.
# Run once after pushing this repo to GitHub:
#
#   bash .github/branch-protection.sh your-org/proxy-hopper
#
# Requires: gh CLI authenticated with repo admin permissions.

set -euo pipefail

REPO="${1:?Usage: $0 <owner/repo>}"

apply_protection() {
  local branch="$1"
  echo "Applying protection to '$branch'..."
  gh api \
    --method PUT \
    -H "Accept: application/vnd.github+json" \
    "/repos/${REPO}/branches/${branch}/protection" \
    --input - <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "Test / Python 3.11",
      "Test / Python 3.12",
      "Test / Python 3.13"
    ]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false
}
JSON
  echo "  ✓ $branch protected"
}

apply_protection "main"
apply_protection "next"

echo ""
echo "Done. Push access to 'main' and 'next' now requires a passing PR."
