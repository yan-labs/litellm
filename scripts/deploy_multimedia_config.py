"""Restricted host-side config transaction for Tokyo's existing containers.

Only approved alias additions pass the in-container validator. No provider or
virtual-key records are read or changed, no reconciler/model call runs, and
readiness uses the existing master identity entirely inside the container.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional


# Match validate_multimedia_config.APPROVED and the in-container readiness names;
# Omni is a config addition only, so this mode still does not reconcile key grants.
ALIASES = {"gemini-omni", "gpt-image-2", "openrouter-tts/*", "openrouter-stt/*"}
MAX_BYTES = 512 * 1024
# No shell expansion or credential output. These are management reads only;
# successful alias presence is not proof of service-key access or model success.
READINESS_CODE = """
import json, os, sys, urllib.request
try:
    key = os.environ['LITELLM_MASTER_KEY']
    def get(path):
        req = urllib.request.Request('http://127.0.0.1:4000' + path,
            headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)
    get('/health/readiness')
    if sys.argv[1] == 'aliases':
        names = [row.get('model_name') for row in get('/model/info').get('data', [])]
        assert all(names.count(n) == 1 for n in
            ('gemini-omni', 'gpt-image-2', 'openrouter-tts/*', 'openrouter-stt/*'))
except Exception:
    sys.exit(1)
"""


def read_regular(path: Path) -> bytes:
    """Read bounded regular files, refusing symlinks and unexpected host paths."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise RuntimeError("invalid_config_file")
        return stream.read(MAX_BYTES + 1)


def validate_in_container(container: str, current: bytes, candidate: bytes) -> List[str]:
    """Use installed PyYAML without adding host dependencies or logging YAML.

    The target environment is derived from the fixed container name chosen in
    `main()` ("litellm" only for --environment prod, "litellm-test"
    otherwise) rather than added as a new parameter, so every existing call
    site (including the transaction tests, which call `deploy()` with a
    hardcoded container name) keeps working unchanged. This lets the in-
    container validator pick the right side of a per-environment APPROVED
    entry (see `validate_multimedia_config.approved_for`) for aliases like
    `gpt-image-2` that must resolve to different upstream values in TEST vs
    PROD.
    """
    environment = "prod" if container == "litellm" else "test"
    source = Path(__file__).with_name("validate_multimedia_config.py").read_text()
    result = subprocess.run(["docker", "exec", "-i", container, "python", "-c", source],
        input=json.dumps({"current": current.decode(), "candidate": candidate.decode(), "environment": environment}),
        capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("config_validation_failed")
    added = json.loads(result.stdout).get("added_aliases")
    if not isinstance(added, list) or not set(added).issubset(ALIASES):
        raise RuntimeError("invalid_validation_result")
    return added


def restart(container: str) -> None:
    """Restart only the selected existing container; do not recreate its env."""
    subprocess.run(["docker", "restart", "--time", "30", container], check=True,
        capture_output=True, timeout=90)


def ready(container: str, require_aliases: bool) -> bool:
    """Bound readiness reads and optionally require exactly one of each alias."""
    for attempt in range(12):
        try:
            result = subprocess.run(["docker", "exec", container, "python", "-c", READINESS_CODE,
                "aliases" if require_aliases else "readiness"], capture_output=True, timeout=15)
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
        if attempt < 11:
            time.sleep(5)
    return False


def replace_config(path: Path, data: bytes, original: os.stat_result) -> None:
    """Atomically replace config preserving its owner/mode, never printing bytes."""
    descriptor, name = tempfile.mkstemp(prefix=".multimedia-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(original.st_mode))
            os.fchown(stream.fileno(), original.st_uid, original.st_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync_directory(directory: Path) -> None:
    """持久化 rename/独占创建的目录项，避免重启后只剩半份发布证据。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_private(path: Path, data: bytes) -> None:
    """独占创建备份或初始 receipt；绝不覆盖已有证据或输出配置内容。"""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def valid_owner(value: object) -> bool:
    """owner 只携带固定发布身份，禁止把不可信字段拼入 receipt 路径。"""
    return value is None or (isinstance(value, dict) and set(value) == {"deployment_id", "expected_sha"}
        and isinstance(value["deployment_id"], str) and re.fullmatch(r"[0-9]+(?:-[0-9]+)?", value["deployment_id"]) is not None
        and isinstance(value["expected_sha"], str) and re.fullmatch(r"[a-f0-9]{40}", value["expected_sha"]) is not None)


def read_owner(path: Path) -> Optional[dict]:
    """精确读取环境 owner；损坏/软链不是无主，必须停止。"""
    if not path.exists() and not path.is_symlink():
        return None
    owner = json.loads(read_regular(path))
    if owner is None or not valid_owner(owner):
        raise RuntimeError("release_ownership_conflict")
    return owner


def require_terminal_owner(directory: Path, container: str, owner: Optional[dict]) -> None:
    """新发布只能接替明确终态的 owner，绝不靠相同配置字节接管未完成发布。"""
    if owner is None:
        return
    path = directory / ("config.yaml.receipt.multimedia-" + owner["deployment_id"] + ".json")
    receipt = json.loads(read_regular(path))
    if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in owner.items()) or receipt.get("phase") not in {"ready", "rolled_back"} or receipt.get("directory") != str(directory.resolve()) or receipt.get("container") != container:
        raise RuntimeError("release_ownership_conflict")


def deploy(directory: Path, container: str, candidate: Path, deployment_id: str, expected_sha: str) -> None:
    """Validate, backup, restart, and compensate failure under a nonblocking lock.

    Rollback only overwrites the candidate this transaction wrote. A foreign
    replacement is a safety stop; the immutable backup remains for its owner.
    The container restart is required so file bind mounts see the new inode.
    A private receipt precedes rename, so SIGKILL recovery needs no exception
    handler. Only the same run/SHA/target and exact backup/candidate may resume.
    """
    if not re.fullmatch(r"[0-9]+(?:-[0-9]+)?", deployment_id):
        raise RuntimeError("invalid_deployment_id")
    if not re.fullmatch(r"[a-f0-9]{40}", expected_sha):
        raise RuntimeError("invalid_expected_sha")
    lock_fd = os.open(directory / ".multimedia-config.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = directory / "config.yaml"
        before, after = read_regular(config), read_regular(candidate)
        original = config.lstat()
        backup = directory / ("config.yaml.bak.multimedia-" + deployment_id)
        receipt_path = directory / ("config.yaml.receipt.multimedia-" + deployment_id + ".json")
        owner_path = directory / ".multimedia-config.owner.json"
        owner = read_owner(owner_path)
        our_owner = {"deployment_id": deployment_id, "expected_sha": expected_sha}
        # receipt 不含配置/凭据，只锚定原发布与不可变字节；其它 run 不能认领它。
        identity = {"version": 1, "deployment_id": deployment_id, "expected_sha": expected_sha,
            "directory": str(directory.resolve()), "container": container,
            "candidate_sha256": hashlib.sha256(after).hexdigest(),
            "uid": original.st_uid, "gid": original.st_gid, "mode": stat.S_IMODE(original.st_mode),
            "previous_owner": owner}
        recovered = receipt_path.exists() or receipt_path.is_symlink()
        phase = "pending"
        if recovered:
            receipt = json.loads(read_regular(receipt_path))
            before = read_regular(backup)
            identity["backup_sha256"] = hashlib.sha256(before).hexdigest()
            phase = receipt.get("phase") if isinstance(receipt, dict) else None
            identity["previous_owner"] = receipt.get("previous_owner") if isinstance(receipt, dict) else None
            if not valid_owner(identity["previous_owner"]):
                raise RuntimeError("recovery_identity_mismatch")
            if phase not in {"pending", "rollback", "ready", "rolled_back"} or receipt != {**identity, "phase": phase}:
                raise RuntimeError("recovery_identity_mismatch")
            # 终态只是历史结果，重跑不允许 restart/rollback；否则旧失败会撤销后续成功。
            if phase in {"ready", "rolled_back"}:
                if owner != our_owner:
                    raise RuntimeError("release_ownership_conflict")
                expected = after if phase == "ready" else before
                if read_regular(config) != expected or not ready(container, phase == "ready"):
                    raise RuntimeError("terminal_release_not_ready")
                if phase == "rolled_back":
                    raise RuntimeError("candidate_failed_rollback_ready")
                print("multimedia_config_ready")
                return
            if owner != our_owner:
                # receipt 已落但 owner 尚未替换时可恢复；前任身份必须仍完全不变且终态。
                if owner != identity["previous_owner"] or read_regular(config) != before:
                    raise RuntimeError("release_ownership_conflict")
                require_terminal_owner(directory, container, owner)
            if read_regular(config) not in (before, after):
                raise RuntimeError("rollback_refused_foreign_config")
        if not recovered:
            require_terminal_owner(directory, container, owner)
            try:
                added = validate_in_container(container, before, after)
            except Exception:
                raise RuntimeError("config_validation_failed") from None
            if not added:
                if not ready(container, True):
                    raise RuntimeError("unchanged_config_not_ready")
                print("multimedia_config_noop")
                return
        # 恢复只接受已验证且摘要完全相同的候选/备份；容器可能正停在 restart 中途，
        # 因此不能要求先 docker exec 校验才允许恢复该容器。
        if not recovered and read_regular(config) != before:
            raise RuntimeError("config_changed_during_validation")
        if not recovered:
            create_private(backup, before)
            identity["backup_sha256"] = hashlib.sha256(before).hexdigest()
            create_private(receipt_path, json.dumps({**identity, "phase": phase}).encode())
        if read_owner(owner_path) != owner:
            raise RuntimeError("release_ownership_conflict")
        if owner != our_owner:
            data = json.dumps(our_owner).encode()
            if owner is None:
                create_private(owner_path, data)
            else:
                replace_config(owner_path, data, owner_path.stat())

        def record_phase(value: str) -> None:
            """先持久化回滚方向，再改配置；恢复进程不能把回滚中断误当新发布。"""
            if read_owner(owner_path) != our_owner:
                raise RuntimeError("release_ownership_conflict")
            replace_config(receipt_path, json.dumps({**identity, "phase": value}).encode(), receipt_path.stat())

        def rollback() -> None:
            """仅恢复本发布候选/备份，重启恢复旧 inode，失败保留可重放方向。"""
            if read_owner(owner_path) != our_owner:
                raise RuntimeError("release_ownership_conflict")
            current = read_regular(config)
            if current not in (before, after):
                raise RuntimeError("rollback_refused_foreign_config")
            if current == after:
                replace_config(config, before, original)
            try:
                restart(container)
                if not ready(container, False):
                    raise RuntimeError("restored_not_ready")
            except Exception:
                raise RuntimeError("rollback_readiness_failed") from None
            record_phase("rolled_back")
            raise RuntimeError("candidate_failed_rollback_ready")

        if phase == "rollback":
            rollback()
        current = read_regular(config)
        if current not in (before, after):
            raise RuntimeError("rollback_refused_foreign_config")
        if current == before:
            replace_config(config, after, original)
        try:
            restart(container)
            if not ready(container, True):
                raise RuntimeError("candidate_not_ready")
        except Exception:
            record_phase("rollback")
            rollback()
        record_phase("ready")
        print("multimedia_config_ready")


def main() -> int:
    """Resolve fixed environment paths, with only static diagnostic output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("test", "prod"), required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    directory, container = (Path("/opt/litellm"), "litellm") if args.environment == "prod" else (Path("/opt/litellm-test"), "litellm-test")
    try:
        deploy(directory, container, args.candidate, args.deployment_id, args.expected_sha)
        return 0
    except Exception as error:
        # Only our fixed codes are safe; subprocess/parser exceptions can contain
        # config text, so never print exception strings outside this whitelist.
        allowed = {"invalid_deployment_id", "invalid_expected_sha", "recovery_identity_mismatch", "release_ownership_conflict", "terminal_release_not_ready", "invalid_config_file", "config_validation_failed", "unchanged_config_not_ready", "config_changed_during_validation", "rollback_refused_foreign_config", "rollback_readiness_failed", "candidate_failed_rollback_ready"}
        code = str(error) if isinstance(error, RuntimeError) and str(error) in allowed else "multimedia_release_failed"
        print(code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
