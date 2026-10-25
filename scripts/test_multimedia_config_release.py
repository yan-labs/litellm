"""Offline release gates for the three existing managed multimedia aliases."""

import base64
import copy
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.validate_multimedia_config import media_routes_for, APPROVED, approved_for, validate, validate_media_routes
from scripts.classify_multimedia_push import CONTROL_PATHS, main as classify_main, should_skip
from scripts.deploy_multimedia_config import READINESS_CODE, deploy, read_regular, validate_in_container
from scripts.dispatch_multimedia_config import remote_command
from scripts import dispatch_multimedia_config as dispatch


class ConfigGateTests(unittest.TestCase):
    """Reject all semantic changes except exact approved alias additions.

    Most cases here only exercise the shared shape of the gate (settings,
    unrelated routes, duplicate keys) and are indifferent to which
    environment they run under, so they default to "test". The
    environment-specific behavior itself (gpt-image-2's diverging upstream)
    has its own dedicated test below.
    """

    def setUp(self):
        self.environment = "test"
        self.current = {"model_list": [{"model_name": "existing", "litellm_params": {"model": "openrouter/existing"}}], "general_settings": {"store_model_in_db": True}}
        self.candidate = copy.deepcopy(self.current)
        self.candidate["model_list"].extend(copy.deepcopy(list(approved_for(self.environment).values())))

    def check(self, current=None, candidate=None, environment=None):
        return validate(yaml.safe_dump(current or self.current), yaml.safe_dump(candidate or self.candidate), environment or self.environment)

    def test_exact_additions_and_idempotence(self):
        self.assertEqual(set(self.check()), set(APPROVED))
        self.assertEqual(self.check(self.candidate, self.candidate), [])

    def test_unrelated_changes_rejected(self):
        mutations = [
            lambda c: c.update(general_settings={"store_model_in_db": False}),
            lambda c: c["model_list"].pop(0),
            lambda c: c["model_list"].append({"model_name": "new-route"}),
            lambda c: c["model_list"][0]["litellm_params"].update(model="other"),
            lambda c: c["model_list"][1]["litellm_params"].update(api_key="DO-NOT-PRINT"),
            lambda c: c["model_list"][1]["litellm_params"].update(litellm_credential_name="other"),
            lambda c: c["model_list"][1]["litellm_params"].update(use_in_pass_through=0),
            lambda c: c["model_list"].append(copy.deepcopy(c["model_list"][1])),
            lambda c: c["model_list"].pop(),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                candidate = copy.deepcopy(self.candidate)
                mutate(candidate)
                with self.assertRaises(ValueError):
                    self.check(candidate=candidate)

    def test_duplicate_yaml_key_and_non_mapping_rejected(self):
        for raw in ["model_list: []\nmodel_list: []", "[]", "!!python/object:unsafe {}"]:
            with self.assertRaises(ValueError):
                validate(raw, yaml.safe_dump(self.candidate), self.environment)

    def test_existing_target_drift_is_not_repaired(self):
        live = copy.deepcopy(self.candidate)
        live["model_list"][1]["litellm_params"]["model"] = "unexpected"
        with self.assertRaises(ValueError):
            self.check(current=live)

    def test_repository_configs_have_exact_routes(self):
        """Each committed config file matches this exact environment's approved rows."""
        root = Path(__file__).resolve().parents[1]
        for env in ("test", "prod"):
            raw = (root / f"config/litellm.{env}.yaml").read_text()
            self.assertEqual(len(validate_media_routes(raw, env)), 8)
            data = yaml.safe_load((root / f"config/litellm.{env}.yaml").read_text())
            by_name = {row["model_name"]: row for row in data["model_list"]}
            for name, expected in approved_for(env).items():
                # Full YAML uses managed routes; remote media-only APPROVED remains fixed.
                if name == "gpt-image-2":
                    expected = media_routes_for(env)[name]
                self.assertEqual(by_name.get(name), expected)

    def test_per_environment_mapping_resolves_and_rejects_foreign_value(self):
        """`approved_for()` must resolve a {"test": ..., "prod": ...} mapping to
        one concrete value per environment, and `validate()` must reject the
        other environment's value even when the alias name and shape match.

        The generic model mapping is exercised independently of `gpt-image-2`, which
        diverged (TEST on the OpenRouter images-endpoint patch ahead of PROD).
        PROD has since received that patch and been verified on it, so
        `gpt-image-2` collapsed to a single plain-string upstream shared by
        both environments and no longer exercises the mapping branch. The
        mapping mechanism itself is still live code (any future alias may
        need to diverge the same way), so this test exercises it directly
        against a synthetic alias instead of depending on production data
        staying divergent forever.
        """
        synthetic = {
            "sample-mapped-alias": {
                "model_name": "sample-mapped-alias",
                "litellm_params": {
                    "model": {
                        "test": "openrouter/openai/test-only-upstream",
                        "prod": "openrouter/openai/prod-only-upstream",
                    },
                    "custom_llm_provider": "openrouter",
                },
                "model_info": {"mode": "chat"},
            },
        }
        base = {"model_list": [], "general_settings": {}}
        with patch("scripts.validate_multimedia_config.APPROVED", synthetic):
            for environment, other in (("test", "prod"), ("prod", "test")):
                own_candidate = copy.deepcopy(base)
                own_candidate["model_list"].extend(copy.deepcopy(list(approved_for(environment).values())))
                added = validate(yaml.safe_dump(base), yaml.safe_dump(own_candidate), environment)
                self.assertEqual(set(added), set(synthetic))
                self.assertEqual(validate(yaml.safe_dump(own_candidate), yaml.safe_dump(own_candidate), environment), [])

                # The other environment's approved upstream must be rejected
                # outright, not silently accepted because the alias name and
                # surrounding structure still match.
                foreign_candidate = copy.deepcopy(base)
                foreign_candidate["model_list"].extend(copy.deepcopy(list(approved_for(other).values())))
                with self.assertRaises(ValueError):
                    validate(yaml.safe_dump(base), yaml.safe_dump(foreign_candidate), environment)

    def test_unknown_environment_rejected(self):
        with self.assertRaises(ValueError):
            validate(yaml.safe_dump(self.current), yaml.safe_dump(self.candidate), "staging")


class PushGateTests(unittest.TestCase):
    """A control-only push must never enter legacy TEST reconciliation."""

    def test_control_only_push_skips(self):
        self.assertTrue(should_skip("push", list(CONTROL_PATHS)))

    def test_other_code_preserves_normal_deployment(self):
        self.assertFalse(should_skip("push", ["litellm/main.py"]))
        self.assertFalse(should_skip("push", [*CONTROL_PATHS, "litellm/main.py"]))
        self.assertFalse(should_skip("push", ["config/litellm.test.yaml"]))

    def test_dispatch_and_unknown_push_fail_closed(self):
        self.assertFalse(should_skip("workflow_dispatch", []))
        with self.assertRaises(ValueError):
            should_skip("push", [])

    def test_unavailable_push_baseline_prevents_output(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push", "PUSH_BEFORE": "0" * 40}), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(classify_main(), 1)
            self.assertEqual(stdout.getvalue(), "")
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "push", "PUSH_BEFORE": "abc"}), patch("scripts.classify_multimedia_push.subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(classify_main(), 1)
            self.assertEqual(stdout.getvalue(), "")

    def test_actual_patch_file_set_follows_control_only_deploy_gate(self):
        root = Path(__file__).resolve().parents[1]
        changed = subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=root).decode().splitlines()
        untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"], cwd=root).decode().splitlines()
        if changed or untracked:
            self.assertEqual(should_skip("push", changed + untracked),
                             set(changed + untracked).issubset(CONTROL_PATHS))

    def test_workflow_gates_before_deploy_and_excludes_legacy_health(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/deploy-tokyo.yml").read_text()
        self.assertIn("if: needs.guard-branch.outputs.skip_deploy != 'true'", workflow)
        self.assertIn("if: steps.mode.outputs.action_mode != 'sync-multimedia-only'", workflow)
        self.assertIn("scripts/test_multimedia_config_release.py", workflow)
        parsed = yaml.safe_load(workflow)
        steps = parsed["jobs"]["deploy"]["steps"]
        for step in steps:
            run = step.get("run", "")
            if "reconcile_" in run and "unittest" not in run:
                self.assertIn("if", step)
                self.assertNotIn("sync-multimedia-only", step["if"])
            if "printenv" in run:
                self.assertEqual(step["if"], "steps.mode.outputs.action_mode != 'sync-multimedia-only'")


class TransactionTests(unittest.TestCase):
    """Use local temporary files and fake Docker; no network/model/credential use."""

    def test_sigkill_after_replace_recovers_without_replacing_backup(self):
        """真实进程在 rename 后被 SIGKILL；下一进程只能恢复同一发布。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_bytes(b"before")
            candidate = root / "candidate.yaml"
            candidate.write_bytes(b"after")
            child = '''
import os, signal, sys
from pathlib import Path
from scripts import deploy_multimedia_config as release
release.validate_in_container = lambda container, before, after: list(release.ALIASES) if before != after else []
release.restart = lambda container: os.kill(os.getpid(), signal.SIGKILL)
release.deploy(Path(sys.argv[1]), 'litellm-test', Path(sys.argv[1]) / 'candidate.yaml', '123-1', 'a' * 40)
'''
            killed = subprocess.run([sys.executable, "-c", child, directory], capture_output=True)
            self.assertEqual(killed.returncode, -signal.SIGKILL)
            self.assertEqual((root / "config.yaml").read_bytes(), b"after")
            with patch("scripts.deploy_multimedia_config.validate_in_container", side_effect=lambda c, a, b: list(APPROVED) if a != b else []), patch("scripts.deploy_multimedia_config.restart") as restart, patch("scripts.deploy_multimedia_config.ready", return_value=True):
                deploy(root, "litellm-test", candidate, "123-1", "a" * 40)
                restart.assert_called_once_with("litellm-test")
            self.assertEqual((root / "config.yaml.bak.multimedia-123-1").read_bytes(), b"before")

    def test_expected_sha_required_exact_and_checked_before_aws(self):
        """受限模式缺少或错 SHA 必须在 AWS 身份与 SSM 之前拒绝。"""
        for sha in ("", "abc", "b" * 40):
            with self.subTest(sha=sha), patch.object(dispatch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "a" * 40 + "\n", "")), patch.object(dispatch, "aws") as aws:
                with self.assertRaises(ValueError):
                    dispatch.verify_expected_sha(sha)
                aws.assert_not_called()
        with patch.object(dispatch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "a" * 40 + "\n", "")):
            self.assertEqual(dispatch.verify_expected_sha("a" * 40), "a" * 40)
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/deploy-tokyo.yml").read_text())
        inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
        self.assertIn("expected_sha", inputs)
        steps = workflow["jobs"]["deploy"]["steps"]
        guard = next(index for index, step in enumerate(steps) if "--verify-only" in step.get("run", ""))
        aws = next(index for index, step in enumerate(steps) if "configure-aws-credentials" in step.get("uses", ""))
        self.assertLess(guard, aws)
        self.assertIn("sync-multimedia-only", steps[guard]["if"])

    def test_interrupted_rollback_and_pre_rename_resume_in_the_recorded_direction(self):
        """真实 SIGKILL 分别打在首次 rename 前和回滚 restart 前，恢复方向不能丢。"""
        for stage in ("before_replace", "rollback_restart"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "config.yaml").write_bytes(b"before")
                candidate = root / "candidate.yaml"
                candidate.write_bytes(b"after")
                child = '''
import os, signal, sys
from pathlib import Path
from scripts import deploy_multimedia_config as release
release.validate_in_container = lambda *args: list(release.ALIASES)
release.ready = lambda container, aliases: not aliases
original = release.replace_config
def replace(path, data, info):
    if sys.argv[2] == 'before_replace' and path.name == 'config.yaml':
        os.kill(os.getpid(), signal.SIGKILL)
    original(path, data, info)
release.replace_config = replace
calls = 0
def restart(container):
    global calls
    calls += 1
    if calls == 2:
        os.kill(os.getpid(), signal.SIGKILL)
release.restart = restart
release.deploy(Path(sys.argv[1]), 'litellm-test', Path(sys.argv[1]) / 'candidate.yaml', '123', 'a' * 40)
'''
                killed = subprocess.run([sys.executable, "-c", child, directory, stage], capture_output=True)
                self.assertEqual(killed.returncode, -signal.SIGKILL)
                self.assertEqual((root / "config.yaml").read_bytes(), b"before")
                with patch("scripts.deploy_multimedia_config.validate_in_container", side_effect=AssertionError("container may be stopped")) as gate, patch("scripts.deploy_multimedia_config.restart") as restart, patch("scripts.deploy_multimedia_config.ready", return_value=True):
                    if stage == "rollback_restart":
                        with self.assertRaisesRegex(RuntimeError, "candidate_failed_rollback_ready"):
                            deploy(root, "litellm-test", candidate, "123", "a" * 40)
                    else:
                        deploy(root, "litellm-test", candidate, "123", "a" * 40)
                    restart.assert_called_once_with("litellm-test")
                    gate.assert_not_called()
                self.assertEqual((root / "config.yaml").read_bytes(), b"before" if stage == "rollback_restart" else b"after")

    def test_recovery_rejects_identity_candidate_backup_and_foreign_config(self):
        """备份/候选字节和目标身份必须全部匹配，拒绝时零 restart、零覆盖。"""
        for changed in ("sha", "container", "candidate", "backup", "config", "run", "receipt", "missing-owner", "foreign-owner"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, candidate = root / "config.yaml", root / "candidate.yaml"
                config.write_bytes(b"before")
                candidate.write_bytes(b"after")
                with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart", side_effect=SystemExit):
                    with self.assertRaises(SystemExit):
                        deploy(root, "litellm-test", candidate, "123", "a" * 40)
                if changed in {"candidate", "backup", "config", "receipt"}:
                    target = {"candidate": candidate, "backup": root / "config.yaml.bak.multimedia-123",
                        "config": config, "receipt": root / "config.yaml.receipt.multimedia-123.json"}[changed]
                    target.write_bytes(b"foreign")
                if changed == "missing-owner":
                    (root / ".multimedia-config.owner.json").unlink()
                if changed == "foreign-owner":
                    (root / ".multimedia-config.owner.json").write_text(json.dumps({"deployment_id": "124", "expected_sha": "a" * 40}))
                original_config = config.read_bytes()
                with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=[]), patch("scripts.deploy_multimedia_config.restart") as restart, patch("scripts.deploy_multimedia_config.ready", return_value=False):
                    with self.assertRaises((RuntimeError, ValueError)):
                        deploy(root, "foreign" if changed == "container" else "litellm-test", candidate,
                            "124" if changed == "run" else "123", ("b" if changed == "sha" else "a") * 40)
                    restart.assert_not_called()
                self.assertEqual(config.read_bytes(), original_config)

    def test_dispatch_missing_and_mismatched_sha_never_calls_aws(self):
        """同时覆盖实际 CLI 入口，避免 helper 正确但 dispatch 绕过门禁。"""
        for arguments in (["--environment", "test"], ["--environment", "prod", "--expected-sha", "b" * 40]):
            with patch.object(sys, "argv", ["dispatch", *arguments]), patch.object(dispatch, "aws") as aws, patch.object(dispatch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "a" * 40 + "\n", "")), contextlib.redirect_stderr(io.StringIO()):
                if "--expected-sha" not in arguments:
                    with self.assertRaises(SystemExit):
                        dispatch.main()
                else:
                    self.assertEqual(dispatch.main(), 1)
                aws.assert_not_called()

    def test_workflow_rerun_uses_same_release_identity_and_sha_for_both_environments(self):
        """attempt 改变不改变发布身份；TEST/PROD payload 始终携带同一显式 SHA。"""
        for environment in ("test", "prod"):
            for attempt in ("1", "2"):
                with patch.object(sys, "argv", ["dispatch", "--environment", environment, "--expected-sha", "a" * 40]), patch.dict(os.environ, {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": attempt, "INSTANCE_ID": "offline", "GITHUB_STEP_SUMMARY": ""}), patch.object(dispatch, "verify_expected_sha", return_value="a" * 40), patch.object(dispatch, "remote_command", return_value="safe-command") as command, patch.object(dispatch, "aws", side_effect=[{"Command": {"CommandId": "offline"}}, {"Status": "Success"}]), patch.object(dispatch.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(dispatch.main(), 0)
                    command.assert_called_once_with(environment, "123", "a" * 40)

    def test_success_noop_rollback_and_foreign_change(self):
        for case in ("success", "noop", "readiness_failure", "foreign_change", "validation_failure"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / "config.yaml"
                config.write_bytes(b"before")
                candidate = root / "candidate.yaml"
                candidate.write_bytes(b"after")
                changes = [] if case == "noop" else list(APPROVED)

                def readiness(container, require_aliases):
                    if require_aliases and case == "foreign_change":
                        config.write_bytes(b"foreign")
                    return not (require_aliases and case in ("readiness_failure", "foreign_change"))

                with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=changes) as gate, patch("scripts.deploy_multimedia_config.restart") as restart, patch("scripts.deploy_multimedia_config.ready", side_effect=readiness):
                    if case == "validation_failure":
                        gate.side_effect = ValueError("rejected")
                    if case in ("readiness_failure", "foreign_change", "validation_failure"):
                        with self.assertRaises(RuntimeError):
                            deploy(root, "litellm-test", candidate, "123-1", "a" * 40)
                    else:
                        deploy(root, "litellm-test", candidate, "123-1", "a" * 40)
                    expected = b"after" if case == "success" else b"foreign" if case == "foreign_change" else b"before"
                    self.assertEqual(config.read_bytes(), expected)
                    self.assertEqual(restart.call_count, {"success": 1, "noop": 0, "readiness_failure": 2, "foreign_change": 1, "validation_failure": 0}[case])
                    if case not in ("noop", "validation_failure"):
                        self.assertEqual((root / "config.yaml.bak.multimedia-123-1").read_bytes(), b"before")

    def test_old_rolled_back_run_cannot_undo_new_success(self):
        """旧失败→新成功→旧重跑，旧 receipt 只能报告结果，不能覆盖新 owner。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, candidate = root / "config.yaml", root / "candidate.yaml"
            config.write_bytes(b"before")
            candidate.write_bytes(b"after")
            with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart"), patch("scripts.deploy_multimedia_config.ready", side_effect=lambda c, aliases: not aliases):
                with self.assertRaisesRegex(RuntimeError, "candidate_failed_rollback_ready"):
                    deploy(root, "litellm-test", candidate, "123", "a" * 40)
            with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart"), patch("scripts.deploy_multimedia_config.ready", return_value=True):
                deploy(root, "litellm-test", candidate, "124", "a" * 40)
            with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart") as restart, patch("scripts.deploy_multimedia_config.ready", return_value=True):
                with self.assertRaises(RuntimeError):
                    deploy(root, "litellm-test", candidate, "123", "a" * 40)
                restart.assert_not_called()
            self.assertEqual(config.read_bytes(), b"after")

    def test_pending_run_blocks_new_run_even_when_candidate_bytes_match(self):
        """未终态 owner 必须先恢复；相同候选字节不能让新 run 冒充已完成。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_bytes(b"before")
            candidate = root / "candidate.yaml"
            candidate.write_bytes(b"after")
            with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart", side_effect=SystemExit):
                with self.assertRaises(SystemExit):
                    deploy(root, "litellm-test", candidate, "123", "a" * 40)
            with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=[]), patch("scripts.deploy_multimedia_config.restart") as restart, patch("scripts.deploy_multimedia_config.ready", return_value=True):
                with self.assertRaises(RuntimeError):
                    deploy(root, "litellm-test", candidate, "124", "a" * 40)
                restart.assert_not_called()
            self.assertFalse((root / "config.yaml.bak.multimedia-124").exists())

    def test_terminal_replays_never_restart_or_rewrite_receipt(self):
        """ready/rolled_back 重放即使当前不就绪也只读失败，不能重新发起事务。"""
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config, candidate = root / "config.yaml", root / "candidate.yaml"
                config.write_bytes(b"before")
                candidate.write_bytes(b"after")
                with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart"), patch("scripts.deploy_multimedia_config.ready", side_effect=lambda c, aliases: success or not aliases):
                    try:
                        deploy(root, "litellm-test", candidate, "123", "a" * 40)
                    except RuntimeError:
                        self.assertFalse(success)
                receipt = root / "config.yaml.receipt.multimedia-123.json"
                saved = receipt.read_bytes()
                with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart") as restart, patch("scripts.deploy_multimedia_config.ready", return_value=False):
                    with self.assertRaises(RuntimeError):
                        deploy(root, "litellm-test", candidate, "123", "a" * 40)
                    restart.assert_not_called()
                self.assertEqual(receipt.read_bytes(), saved)
                self.assertEqual(config.read_bytes(), b"after" if success else b"before")

    def test_rejects_symlink_and_existing_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_bytes(b"before")
            candidate = root / "candidate.yaml"
            candidate.symlink_to(config)
            with self.assertRaises(OSError):
                read_regular(candidate)
            candidate.unlink()
            candidate.write_bytes(b"after")
            (root / "config.yaml.bak.multimedia-123-1").write_bytes(b"protected")
            with patch("scripts.deploy_multimedia_config.validate_in_container", return_value=list(APPROVED)), patch("scripts.deploy_multimedia_config.restart") as restart:
                with self.assertRaises(FileExistsError):
                    deploy(root, "litellm-test", candidate, "123-1", "a" * 40)
                restart.assert_not_called()
                self.assertEqual(config.read_bytes(), b"before")
                self.assertEqual((root / "config.yaml.bak.multimedia-123-1").read_bytes(), b"protected")

    def test_container_validator_and_health_never_invoke_models(self):
        returned = subprocess.CompletedProcess([], 0, json.dumps({"added_aliases": list(APPROVED)}), "")
        with patch("scripts.deploy_multimedia_config.subprocess.run", return_value=returned) as run:
            validate_in_container("litellm-test", b"before", b"after")
            args, kwargs = run.call_args
            self.assertEqual(args[0][:5], ["docker", "exec", "-i", "litellm-test", "python"])
            self.assertNotIn("before", " ".join(args[0]))
            self.assertEqual(json.loads(kwargs["input"])["current"], "before")
        for denied in ("/key/", "/credentials", "/images/generations", "/chat/completions", "/audio/"):
            self.assertNotIn(denied, READINESS_CODE)
        self.assertIn("/model/info", READINESS_CODE)
        self.assertIn("/health/readiness", READINESS_CODE)

    def test_remote_upload_is_syntax_valid_and_bounded(self):
        command = remote_command("prod", "123-1", "a" * 40)
        encoded = command.split("base64.b64decode('")[1].split("')")[0]
        installer = zlib.decompress(base64.b64decode(encoded)).decode()
        compile(installer, "remote-installer", "exec")
        payload = installer.split("files=json.loads(zlib.decompress(base64.b64decode('")[1].split("')))")[0]
        files = json.loads(zlib.decompress(base64.b64decode(payload)))
        root = Path(__file__).resolve().parents[1]
        for name, source in {"candidate.yaml": "config/litellm.prod.yaml",
            "deploy_multimedia_config.py": "scripts/deploy_multimedia_config.py",
            "validate_multimedia_config.py": "scripts/validate_multimedia_config.py"}.items():
            self.assertEqual(files[name].encode("utf-8"), (root / source).read_bytes())
        # Keep a conservative command-size budget rather than relying on SSM's
        # remaining document/request envelope capacity.
        self.assertLess(len(command), 24000)
        self.assertNotIn("reconcile_", installer)


if __name__ == "__main__":
    unittest.main()
