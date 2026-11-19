"""Install a tested project release in WSL while preserving user config and state."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import sqlite3
import subprocess
import sys
import tarfile
import zipfile
import time
from datetime import datetime, timezone

HOME = Path("/home/lan_z")
DEPLOY = HOME / "company-agent-deploy"
CONFIG = HOME / ".config/company-agent/runtime.env"
DATA = HOME / ".local/share/company-agent"
STATE = HOME / "company-agent-step0/superagent-state"
OLD_CODE = HOME / "company-agent-step0/dsh-superagent"
SYSTEMD = Path("/etc/systemd/system")
EGRESS_SCRIPT = Path("/usr/local/sbin/company-agent-anythingllm-egress")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stderr[-2000:]}")
    return result


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value
    return values


def validate_runtime_config(path: Path = CONFIG) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"private runtime config is missing: {path}")
    values = read_env(path)
    if not values.get("COMPANY_AGENT_ADMIN_TOKEN"):
        raise RuntimeError("admin token is missing from the private runtime config")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("private runtime config must not be group/world-readable")
    return values


def snapshot_sqlite(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst, pages=256, sleep=0.05)
    finally:
        dst.close()
        src.close()
    os.chmod(destination, 0o600)


def stop_legacy() -> None:
    candidates = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            cmd = (entry / "cmdline").read_bytes().decode(errors="replace").replace("\0", " ")
            cwd = os.readlink(entry / "cwd")
        except (OSError, PermissionError):
            continue
        company_data = "/home/lan_z/.local/share/company-agent"
        if (("/tmp/company-agent-run-bin/dsh --profile superagent" in cmd)
                or (" -m app " in f" {cmd} " and company_data in cmd
                    and ("qq-serve" in cmd or "admin-serve" in cmd))
                or ("node " in cmd and "/dsh-superagent/" in cwd)):
            candidates[pid] = cmd[:240]
    for pid in candidates:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 20
    while candidates and time.monotonic() < deadline:
        candidates = {pid: cmd for pid, cmd in candidates.items() if Path(f"/proc/{pid}").exists()}
        time.sleep(0.25)
    for pid in candidates:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def safe_extract(archive: Path, release: Path) -> None:
    release.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (release / member.name).resolve()
            if release.resolve() not in target.parents and target != release.resolve():
                raise RuntimeError("release archive contains an unsafe path")
        tar.extractall(release, members=members, filter="data")
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["artifacts"].items():
        path = release / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise RuntimeError(f"release artifact hash mismatch: {name}")


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("run this deployment installer as WSL root")
    archive = Path(sys.argv[1]).resolve()
    if not archive.is_file():
        raise SystemExit("release archive was not found")
    validate_runtime_config()
    release_id = archive.name.removesuffix(".tar.gz")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = DEPLOY / "backups" / stamp
    backup.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CONFIG, backup / "runtime.env.before")
    shutil.chown(backup, user="lan_z", group="lan_z")
    for path in backup.iterdir():
        shutil.chown(path, user="lan_z", group="lan_z")
        os.chmod(path, 0o600)

    # Extract and validate the whole immutable release before stopping any service.
    releases = DEPLOY / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / release_id
    safe_extract(archive, release)
    wheel = next((release / "company").glob("*.whl"))
    package_root = release / "company"
    with zipfile.ZipFile(wheel) as package:
        for member in package.infolist():
            target = (package_root / member.filename).resolve()
            if package_root.resolve() not in target.parents and target != package_root.resolve():
                raise RuntimeError("Company Agent wheel contains an unsafe path")
        package.extractall(package_root)
    metadata = json.loads((release / "runtime.json").read_text(encoding="utf-8"))
    node = Path(metadata["node"]).resolve(strict=True)
    dsh_bin = Path(metadata["dsh_bin"]).resolve(strict=True)
    dsh_modules = Path(metadata["dsh_node_modules"]).resolve(strict=True)
    dsh_home = Path(metadata["dsh_home"]).resolve(strict=True)
    if not node.is_file() or not dsh_bin.is_file() or not dsh_home.is_dir() or dsh_modules.name != "node_modules":
        raise RuntimeError("release runtime paths are incomplete or invalid")
    if str(dsh_bin).startswith("/tmp/"):
        raise RuntimeError("release DSH entrypoint must use its stable installation path")
    if not (release / "run-release.py").is_file() or not (package_root / "web/index.html").is_file():
        raise RuntimeError("release is missing the Company Agent launcher or console")

    for unit in ["company-agent.target", "company-agent-qq.service", "company-agent-superagent.service", "company-agent@admin.service"]:
        run("systemctl", "stop", unit, check=False)
    stop_legacy()
    snapshot_sqlite(DATA / "control.sqlite", backup / "company-agent.sqlite")
    snapshot_sqlite(STATE / "orchestrator.sqlite", backup / "dssuperagent.sqlite")

    dss_node_modules = release / "dss/node_modules"
    dss_node_modules.symlink_to(dsh_modules, target_is_directory=True)

    current = DEPLOY / "current"
    temporary_link = DEPLOY / "current.next"
    if temporary_link.exists() or temporary_link.is_symlink():
        temporary_link.unlink()
    temporary_link.symlink_to(release, target_is_directory=True)
    if current.exists() and not current.is_symlink():
        raise RuntimeError("deployment current path exists and is not a symlink")
    os.replace(temporary_link, current)

    if OLD_CODE.exists() and not OLD_CODE.is_symlink():
        if OLD_CODE.resolve() != Path("/home/lan_z/company-agent-step0/dsh-superagent"):
            raise RuntimeError("refusing to remove unexpected legacy directory")
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    cwd = os.readlink(entry / "cwd")
                except OSError:
                    continue
                if cwd == str(OLD_CODE) or cwd.startswith(str(OLD_CODE) + "/"):
                    raise RuntimeError("legacy code directory is still in use")
        artifacts = OLD_CODE / ".artifacts"
        if artifacts.exists():
            archived = DATA / "legacy-dsh-artifacts" / stamp
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(artifacts), str(archived))
        shutil.rmtree(OLD_CODE)
    elif OLD_CODE.is_symlink():
        OLD_CODE.unlink()
    OLD_CODE.parent.mkdir(parents=True, exist_ok=True)
    OLD_CODE.symlink_to(f"{DEPLOY}/current/dss", target_is_directory=True)

    shutil.copy2(release / "anythingllm/apply-egress-firewall.sh", EGRESS_SCRIPT)
    os.chmod(EGRESS_SCRIPT, 0o755)
    shutil.copy2(release / "anythingllm/anythingllm-egress-firewall.service", SYSTEMD / "anythingllm-egress-firewall.service")
    for name in ["company-agent@.service", "company-agent-qq.service", "company-agent-superagent.service", "company-agent.target"]:
        shutil.copy2(release / "systemd" / name, SYSTEMD / name)
    run("systemctl", "daemon-reload")
    compose = ["docker", "compose", "--project-name", "company-agent-search", "--env-file",
               str(HOME / ".config/company-agent/search.env"), "-f", str(current / "search/compose.yaml")]
    run(*compose, "up", "-d")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "anythingllm-egress-firewall.service")
    run("systemctl", "restart", "anythingllm-egress-firewall.service")
    run("systemctl", "enable", "company-agent.target")
    run("systemctl", "start", "company-agent.target")
    print(json.dumps({"release": str(release), "backup": str(backup), "current": str(current.resolve())}))


if __name__ == "__main__":
    main()
