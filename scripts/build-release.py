"""Build an immutable deployment archive from the project source checkouts."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def posix_runtime_path(value: str, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute WSL/Linux path")
    return path


def runtime_metadata(node: str, dsh_bin: str, dsh_home: str) -> dict[str, str]:
    node_path = posix_runtime_path(node, "Node executable")
    dsh_path = posix_runtime_path(dsh_bin, "DSH entrypoint")
    home_path = posix_runtime_path(dsh_home, "DSH home")
    if dsh_path.parts[:2] == ("/", "tmp"):
        raise ValueError("the release must reference the stable DSH package entrypoint, not a /tmp alias")
    modules_root = next((parent for parent in dsh_path.parents if parent.name == "node_modules"), None)
    if modules_root is None:
        raise ValueError("DSH entrypoint must be inside a node_modules package")
    return {
        "node": str(node_path),
        "dsh_bin": str(dsh_path),
        "dsh_node_modules": str(modules_root),
        "dsh_home": str(home_path),
    }


def source_manifest(root: Path) -> dict:
    names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
    tracked = {}
    for name in sorted(set(names)):
        path = root / name
        if name and path.is_file() and not name.endswith(".tar.xz"):
            tracked[name] = digest(path)
    return {
        "root": str(root),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "dirty": subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True).splitlines(),
        "files": tracked,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dss-source", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--node", required=True)
    parser.add_argument("--dsh-bin", required=True)
    parser.add_argument("--dsh-home", required=True)
    args = parser.parse_args()
    company = Path(__file__).resolve().parents[1]
    dss = args.dss_source.resolve()
    wheel = args.wheel.resolve()
    release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = company / "build" / "releases"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"release_id": release_id, "company_agent": source_manifest(company), "dssuperagent": source_manifest(dss), "artifacts": {}}
    archive_path = out / (release_id + ".tar.gz")
    with tarfile.open(archive_path, "w:gz") as archive:
        def add(path: Path, name: str):
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file() and "__pycache__" not in child.parts and child.suffix != ".pyc":
                        add(child, name + "/" + child.relative_to(path).as_posix())
            else:
                archive.add(path, arcname=name, recursive=False)
                manifest["artifacts"][name] = digest(path)
        add(wheel, "company/" + wheel.name)
        add(company / "web" / "index.html", "company/web/index.html")
        for name in ["lib", "package.json", "cordis.patch.yml", "locale", "prompts", "skills"]:
            add(dss / name, "dss/" + name)
        add(company / "scripts" / "run-release.py", "run-release.py")
        add(company / "deploy" / "search", "search")
        for service in ["company-agent@.service", "company-agent-qq.service", "company-agent-superagent.service", "company-agent.target"]:
            add(company / "deploy" / "systemd" / service, "systemd/" + service)
        add(company / "deploy" / "anythingllm" / "apply-egress-firewall.sh", "anythingllm/apply-egress-firewall.sh")
        add(company / "deploy" / "anythingllm" / "anythingllm-egress-firewall.service", "anythingllm/anythingllm-egress-firewall.service")
        runtime = runtime_metadata(args.node, args.dsh_bin, args.dsh_home)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "runtime.json"
            path.write_text(json.dumps(runtime, indent=2), encoding="utf-8")
            archive.add(path, arcname="runtime.json")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            archive.add(path, arcname="manifest.json")
    manifest_path = out / (release_id + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"archive": str(archive_path), "manifest": str(manifest_path), "release_id": release_id}))
