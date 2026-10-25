"""Keep release-control-only pushes away from legacy automatic reconcilers.

This guard runs before the deploy job acquires AWS credentials. Restricted
control changes require an explicit sync-multimedia-only dispatch. Mixed normal
runtime changes retain the repository's existing automatic release contract.
"""

import os
import subprocess
import sys
from typing import List


CONTROL_PATHS = frozenset({
    ".github/workflows/deploy-tokyo.yml",
    "config/litellm.prod.yaml",
    "scripts/classify_multimedia_push.py",
    "scripts/validate_multimedia_config.py",
    "scripts/deploy_multimedia_config.py",
    "scripts/dispatch_multimedia_config.py",
    "scripts/test_multimedia_config_release.py",
    "scripts/test_openrouter_audio_gateway.py",
    "docs/litellm_tokyo_deployment.md",
    "tests/test_litellm/test_kollab_tokyo_model_routes.py",
})


def should_skip(event: str, paths: List[str]) -> bool:
    """Fail closed on unprovable push scope; explicit dispatch is not skipped."""
    if event == "workflow_dispatch":
        return False
    if event != "push" or not paths:
        raise ValueError("unproven_push_scope")
    return set(paths).issubset(CONTROL_PATHS)


def main() -> int:
    """Read the exact GitHub push range without printing its file contents."""
    try:
        event = os.environ["GITHUB_EVENT_NAME"]
        paths = []
        if event == "push":
            before = os.environ["PUSH_BEFORE"]
            if not before or set(before) == {"0"}:
                raise ValueError("missing_baseline")
            result = subprocess.run(["git", "diff", "--name-only", "-z", before, "HEAD"], check=True, capture_output=True)
            paths = result.stdout.decode().rstrip("\0").split("\0") if result.stdout else []
        skipped = should_skip(event, paths)
        print("skip_deploy=" + str(skipped).lower())
        return 0
    except Exception:
        print("Cannot prove push scope; deployment refused.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
