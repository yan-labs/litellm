#!/usr/bin/env python3
"""Regression tests for Tokyo deploy-mode classification.

The deploy workflow runs this dependency-free suite before selecting an action
mode. Temporary Git histories cover both push and workflow_dispatch boundaries.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("classify_deploy_action_mode.sh")
WORKFLOW = Path(__file__).parents[1] / ".github/workflows/deploy-tokyo.yml"


class DeployActionModeClassifierTest(unittest.TestCase):
    """Protect the release boundary between config sync and image rebuilds."""

    def setUp(self) -> None:
        """Create an isolated Git repository used by each classification case."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name)
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Deploy Test")
        self._commit_file("README.md", "base\n", "base")
        self.base = self._git("rev-parse", "HEAD")

    def tearDown(self) -> None:
        """Remove the isolated repository after each test."""
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> str:
        """Run Git inside the isolated repository and return trimmed stdout."""
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, text=True
        ).strip()

    def _commit_file(self, relative_path: str, content: str, message: str) -> None:
        """Write and commit one path so tests can construct exact histories."""
        target = self.repo / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self._git("add", relative_path)
        self._git("commit", "-qm", message)

    def _classify(self, before_ref: str, head_ref: str = "HEAD") -> str:
        """Invoke the production classifier against the isolated history."""
        return subprocess.check_output(
            [str(SCRIPT), before_ref, head_ref],
            cwd=self.repo,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()

    def test_config_and_workflow_only_changes_use_sync(self) -> None:
        """Configuration and workflow-only commits keep the fast restart path."""
        self._commit_file("config/litellm.test.yaml", "model_list: []\n", "config")
        self.assertEqual(self._classify(self.base), "sync-and-restart")

        config_head = self._git("rev-parse", "HEAD")
        self._commit_file(".github/workflows/deploy-tokyo.yml", "name: test\n", "workflow")
        self.assertEqual(self._classify(config_head), "sync-and-restart")

    def test_runtime_and_dependency_changes_require_build(self) -> None:
        """Nested runtime, dependency, and overlay paths rebuild the image."""
        for relative_path in (
            "litellm/router.py",
            "pyproject.toml",
            "requirements-dev.txt",
            "deploy/Dockerfile.overlay",
        ):
            before = self._git("rev-parse", "HEAD")
            self._commit_file(relative_path, f"{relative_path}\n", relative_path)
            self.assertEqual(self._classify(before), "build-and-restart")

    def test_multi_commit_push_uses_full_changed_range(self) -> None:
        """A config commit followed by runtime code still requires a build."""
        self._commit_file("config/litellm.test.yaml", "model_list: []\n", "config")
        self._commit_file("litellm/router.py", "runtime\n", "runtime")
        self.assertEqual(self._classify(self.base), "build-and-restart")

    def test_workflow_dispatch_fallback_compares_previous_commit(self) -> None:
        """The HEAD~1 fallback used by manual auto mode classifies code safely."""
        self._commit_file("litellm/router.py", "runtime\n", "runtime")
        self.assertEqual(self._classify("HEAD~1"), "build-and-restart")

    def test_missing_diff_base_fails_closed_to_build(self) -> None:
        """An unavailable event.before cannot silently select config-only sync."""
        self.assertEqual(self._classify("missing-push-base"), "build-and-restart")

    def test_full_build_carries_and_installs_target_environment_config(self) -> None:
        """A code+config release cannot start the new image with stale routes."""
        workflow = WORKFLOW.read_text()
        build_upload = workflow.split("- name: 打包代码 → S3", 1)[1].split(
            "- name: SSM 拉 S3 + docker build + 替换容器", 1
        )[0]
        build_remote = workflow.split(
            "- name: SSM 拉 S3 + docker build + 替换容器", 1
        )[1].split("- name: SSM 仅重启", 1)[0]
        self.assertIn("CONFIG_SRC: ${{ steps.target.outputs.config_src }}", build_upload)
        self.assertIn('"$CONFIG_SRC"', build_upload)
        self.assertIn("CONFIG_SRC='${{ steps.target.outputs.config_src }}'", build_remote)
        self.assertIn('DEPLOY_ID="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"', build_remote)
        self.assertIn('BAK_NAME="${CONTAINER}-bak-${DEPLOY_ID}"', build_remote)
        self.assertIn(
            'STAGING_DIR="/tmp/litellm-overlay-staging-${DEPLOY_ID}"', build_remote
        )
        self.assertNotIn("rm -rf /tmp/litellm-overlay-staging\n", build_remote)
        self.assertIn('"\\$REMOTE_DIR/config.yaml"', build_remote)
        self.assertIn('"\\$STAGING_DIR/\\$CONFIG_SRC"', build_remote)
        self.assertLess(
            build_remote.index('docker build -t "\\$IMAGE_REPO:\\$NEW_TAG"'),
            build_remote.index('sudo cp "\\$REMOTE_DIR/config.yaml" "\\$CONFIG_BACKUP"'),
        )
        self.assertIn('sudo mv "\\$CONFIG_NEXT" "\\$REMOTE_DIR/config.yaml"', build_remote)
        self.assertIn("config_src=config/litellm.test.yaml", workflow)
        self.assertIn("config_src=config/litellm.prod.yaml", workflow)
        rollback = workflow.split("- name: 自动回滚（build-and-restart 失败时）", 1)[
            1
        ].split("- name: 回滚提示（sync-and-restart）", 1)[0]
        self.assertIn("config.yaml.bak.${DEPLOY_ID}", rollback)
        self.assertNotIn("config.yaml.bak.*", rollback)
        self.assertNotIn("LATEST_CONFIG_BACKUP", rollback)
        self.assertLess(rollback.index("cancel-command"), rollback.index("send-command"))
        self.assertLess(rollback.index("BUILD_STATUS"), rollback.index("send-command"))
        self.assertLess(rollback.index("docker inspect"), rollback.index("docker stop"))
        self.assertIn("docker update --restart unless-stopped", rollback)
        self.assertNotIn("docker update --restart unless-stopped '${CONTAINER}' >/dev/null || true", rollback)
        self.assertIn('\\"set -e\\"', rollback)
        self.assertIn("grep -qx 'true unless-stopped'", rollback)
        self.assertIn('if [ "$S" != "Success" ]; then', rollback)


if __name__ == "__main__":
    unittest.main()
