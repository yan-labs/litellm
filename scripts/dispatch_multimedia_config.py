"""Dispatch the bounded config transaction through SSM without legacy steps.

Only repository config and release scripts are uploaded; command output is
reduced to fixed transaction codes. Poll timeout never cancels an in-flight
rollback. The workflow receipt retains the SSM ID for authoritative readback.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path


SAFE_CODES = {
    "multimedia_config_noop", "multimedia_config_ready", "invalid_deployment_id",
    "invalid_config_file", "config_validation_failed", "unchanged_config_not_ready",
    "config_changed_during_validation", "rollback_refused_foreign_config",
    "rollback_readiness_failed", "candidate_failed_rollback_ready", "multimedia_release_failed",
    "invalid_expected_sha", "recovery_identity_mismatch",
    "release_ownership_conflict", "terminal_release_not_ready",
}


def aws(arguments: list) -> dict:
    """Capture AWS responses without dumping errors or remote script content."""
    result = subprocess.run(["aws", "ssm", *arguments, "--output", "json"], check=True,
        capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def verify_expected_sha(expected_sha: str) -> str:
    """受限发布必须绑定完整 checkout SHA；此检查独立运行在获取 AWS 身份之前。"""
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
        raise ValueError("invalid_expected_sha")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
        check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    if head != expected_sha:
        raise ValueError("expected_sha_mismatch")
    return head


def remote_command(environment: str, deployment_id: str, expected_sha: str) -> str:
    """Build private, temporary upload staging with automatic scoped cleanup."""
    if environment not in {"test", "prod"} or not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", deployment_id) or not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
        raise ValueError("invalid_release_identity")
    root = Path(__file__).resolve().parents[1]
    files = {
        "candidate.yaml": (root / f"config/litellm.{environment}.yaml").read_bytes(),
        "deploy_multimedia_config.py": (root / "scripts/deploy_multimedia_config.py").read_bytes(),
        "validate_multimedia_config.py": (root / "scripts/validate_multimedia_config.py").read_bytes(),
    }
    # 配置增长后同时压缩文件包和 installer，保持原 SSM 上传长度上限。
    payload = base64.b64encode(zlib.compress(json.dumps({name: data.decode("utf-8") for name, data in files.items()}, ensure_ascii=False).encode("utf-8"), level=9)).decode()
    # All interpolations are fixed choices, numeric run IDs or base64, never
    # shell input or credentials. Python owns staging cleanup even on failure.
    installer = (
        "import base64,json,pathlib,subprocess,tempfile,zlib\n"
        f"files=json.loads(zlib.decompress(base64.b64decode('{payload}')))\n"
        "with tempfile.TemporaryDirectory(prefix='litellm-multimedia-') as d:\n"
        " for n,v in files.items():\n"
        "  p=pathlib.Path(d)/n; p.write_bytes(v.encode('utf-8')); p.chmod(0o600)\n"
        f" result=subprocess.run(['python3',d+'/deploy_multimedia_config.py','--environment','{environment}','--candidate',d+'/candidate.yaml','--deployment-id','{deployment_id}','--expected-sha','{expected_sha}'])\n"
        " raise SystemExit(result.returncode)\n"
    )
    encoded = base64.b64encode(zlib.compress(installer.encode(), level=9)).decode()
    command = "sudo python3 -c \"import base64,zlib; exec(zlib.decompress(base64.b64decode('" + encoded + "')))\""
    if len(command) > 24000:
        raise ValueError("upload_too_large")
    return command


def main() -> int:
    """Submit once, poll finite status, and report only safe remote result codes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("test", "prod"), required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        expected_sha = verify_expected_sha(args.expected_sha)
        if args.verify_only:
            return 0
        # 重跑原 workflow 保持 run 身份；attempt 只代表一次执行，不得生成新的恢复身份。
        deployment_id = os.environ["GITHUB_RUN_ID"]
        if not re.fullmatch(r"[0-9]+", deployment_id):
            raise ValueError("invalid_deployment_id")
        instance = os.environ["INSTANCE_ID"]
        parameters = {"commands": [remote_command(args.environment, deployment_id, expected_sha)], "executionTimeout": ["900"]}
        with tempfile.TemporaryDirectory(prefix="litellm-multimedia-ssm-") as directory:
            parameter_file = Path(directory) / "parameters.json"
            parameter_file.write_text(json.dumps(parameters))
            parameter_file.chmod(0o600)
            response = aws(["send-command", "--instance-ids", instance, "--document-name", "AWS-RunShellScript",
                "--comment", f"multimedia-config-only {args.environment} {deployment_id}",
                "--parameters", "file://" + str(parameter_file)])
        command_id = response["Command"]["CommandId"]
        print("SSM command_id=" + command_id, flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as stream:
                stream.write(f"\nMultimedia config-only: `{args.environment}`, SHA `{expected_sha}`, run `{deployment_id}`, SSM `{command_id}`.\n")
        for _ in range(120):
            time.sleep(10)
            try:
                result = aws(["get-command-invocation", "--command-id", command_id, "--instance-id", instance])
            except Exception:
                continue
            status = result.get("Status")
            if status in {"Success", "Failed", "Cancelled", "TimedOut", "Cancelling"}:
                for field in ("StandardOutputContent", "StandardErrorContent"):
                    for line in result.get(field, "").splitlines():
                        if line.strip() in SAFE_CODES:
                            print(line.strip())
                print("SSM status=" + status)
                return 0 if status == "Success" else 1
        print("SSM status unknown; do not redispatch or cancel before command readback.")
        return 1
    except Exception:
        print("multimedia_dispatch_failed; inspect SSM status without raw output.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
