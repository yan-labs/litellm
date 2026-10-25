#!/usr/bin/env bash
# Classify a Tokyo deployment as config-only sync or a full image rebuild.
# The deploy workflow calls this after checkout; changed paths are derived from
# Git so push and workflow_dispatch use one fail-closed decision point.

set -euo pipefail

before_ref="${1:?before ref is required}"
head_ref="${2:-HEAD}"

# Resolve the complete changed-file set. An unavailable push base is unsafe to
# classify as config-only, so a diff failure deliberately selects a full build.
if ! changed_files=$(git diff --name-only "$before_ref" "$head_ref"); then
  echo "Unable to diff $before_ref..$head_ref; selecting full build" >&2
  echo "build-and-restart"
  exit 0
fi

echo "Changed files (vs $before_ref):" >&2
sed 's/^/  /' <<< "$changed_files" >&2

# Only the explicit configuration/workflow-only remainder may use the fast
# path. Runtime code, dependency, or overlay changes always rebuild the image.
mode="sync-and-restart"
while IFS= read -r changed_file; do
  case "$changed_file" in
    litellm/*|pyproject.toml|requirements*.txt|deploy/Dockerfile.overlay)
      mode="build-and-restart"
      break
      ;;
  esac
done <<< "$changed_files"

echo "$mode"
