from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SHARED_TOOL_LAYERS = ("common", "internal")
SCRIPT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PACKAGE_SPEC_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]<>=!,~+\-]{0,127}$")
MAX_SCRIPT_CHARS = 256_000


class SharedToolsError(ValueError):
    """Raised when a shared tool layer request or artifact is invalid."""


class SharedToolsManager:
    """Manage Admin-curated common and internal tool/package layers mounted read-only into DSH."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._secure_dir(self.root)

    def ensure_layers(self) -> None:
        for layer in SHARED_TOOL_LAYERS:
            self._ensure_layer(layer)

    def _ensure_layer(self, layer: str) -> Path:
        if layer not in SHARED_TOOL_LAYERS:
            raise SharedToolsError(f"unsupported tool layer: {layer}")
        layer_dir = self.root / layer
        if layer_dir.is_symlink():
            raise SharedToolsError(f"tool layer cannot be a symbolic link: {layer_dir}")
        layer_dir.mkdir(parents=True, exist_ok=True)
        self._secure_dir(layer_dir)
        for sub in ("bin", "python", "scripts"):
            sub_dir = layer_dir / sub
            if sub_dir.is_symlink():
                raise SharedToolsError(f"tool subdirectory cannot be a symbolic link: {sub_dir}")
            sub_dir.mkdir(parents=True, exist_ok=True)
            self._secure_dir(sub_dir)
        return layer_dir

    def _manifest_path(self, layer: str) -> Path:
        return self._ensure_layer(layer) / "manifest.json"

    def _load_manifest(self, layer: str) -> dict[str, Any]:
        path = self._manifest_path(layer)
        if path.is_symlink():
            raise SharedToolsError(f"manifest cannot be a symbolic link: {path}")
        if not path.is_file():
            return {"layer": layer, "packages": [], "scripts": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SharedToolsError(f"corrupt tool manifest for {layer}") from error
        if not isinstance(data, dict):
            return {"layer": layer, "packages": [], "scripts": {}}
        return {
            "layer": layer,
            "packages": list(data.get("packages") or []),
            "scripts": dict(data.get("scripts") or {}),
        }

    def _save_manifest(self, layer: str, manifest: dict[str, Any]) -> None:
        path = self._manifest_path(layer)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            tmp.chmod(0o600)
        tmp.replace(path)
        if os.name != "nt":
            path.chmod(0o600)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"root": str(self.root), "layers": {}}
        for layer in SHARED_TOOL_LAYERS:
            layer_dir = self._ensure_layer(layer)
            manifest = self._load_manifest(layer)
            bin_dir = layer_dir / "bin"
            scripts: list[dict[str, Any]] = []
            for entry in sorted(bin_dir.iterdir(), key=lambda p: p.name):
                if entry.is_symlink() or not entry.is_file():
                    continue
                meta = manifest["scripts"].get(entry.name, {})
                scripts.append({
                    "name": entry.name,
                    "bytes": entry.stat().st_size,
                    "description": str(meta.get("description", "")),
                    "updated_at": str(meta.get("updated_at", "")),
                })
            python_dir = layer_dir / "python"
            dist_infos = sorted(
                p.name for p in python_dir.iterdir()
                if not p.is_symlink() and p.is_dir() and p.name.endswith(".dist-info")
            ) if python_dir.is_dir() else []
            result["layers"][layer] = {
                "path": str(layer_dir),
                "scripts": scripts,
                "packages": manifest["packages"],
                "installed_dists": dist_infos[:200],
            }
        return result

    def read_script(self, layer: str, name: str) -> dict[str, Any]:
        layer_dir = self._ensure_layer(layer)
        if not isinstance(name, str) or not SCRIPT_NAME_PATTERN.fullmatch(name):
            raise SharedToolsError("invalid script name")
        target = (layer_dir / "bin" / name).resolve()
        if not target.is_relative_to((layer_dir / "bin").resolve()) or not target.is_file() or target.is_symlink():
            raise FileNotFoundError(f"script not found: {layer}/{name}")
        manifest = self._load_manifest(layer)
        meta = manifest["scripts"].get(name, {})
        return {
            "layer": layer,
            "name": name,
            "content": target.read_text(encoding="utf-8"),
            "description": str(meta.get("description", "")),
            "updated_at": str(meta.get("updated_at", "")),
        }

    def save_script(self, layer: str, name: str, content: str, *, description: str = "") -> dict[str, Any]:
        layer_dir = self._ensure_layer(layer)
        if not isinstance(name, str) or not SCRIPT_NAME_PATTERN.fullmatch(name):
            raise SharedToolsError("script name must be 1-64 alphanumeric, dot, underscore, or dash characters")
        if not isinstance(content, str) or not content.strip():
            raise SharedToolsError("script content must not be empty")
        if len(content) > MAX_SCRIPT_CHARS:
            raise SharedToolsError(f"script content exceeds {MAX_SCRIPT_CHARS} characters")
        target = layer_dir / "bin" / name
        if target.is_symlink():
            raise SharedToolsError("script target cannot be a symbolic link")
        normalized = content.replace("\r\n", "\n")
        if not normalized.endswith("\n"):
            normalized += "\n"
        tmp = target.with_name(f".{name}.tmp")
        tmp.write_text(normalized, encoding="utf-8")
        if os.name != "nt":
            tmp.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        tmp.replace(target)
        if os.name != "nt":
            target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        manifest = self._load_manifest(layer)
        now = _now()
        manifest["scripts"][name] = {
            "description": str(description or "").strip()[:200],
            "updated_at": now,
        }
        self._save_manifest(layer, manifest)
        return {
            "layer": layer,
            "name": name,
            "path": str(target),
            "bytes": target.stat().st_size,
            "description": manifest["scripts"][name]["description"],
            "updated_at": now,
        }

    def delete_script(self, layer: str, name: str) -> bool:
        layer_dir = self._ensure_layer(layer)
        if not isinstance(name, str) or not SCRIPT_NAME_PATTERN.fullmatch(name):
            raise SharedToolsError("invalid script name")
        target = layer_dir / "bin" / name
        if target.is_symlink():
            raise SharedToolsError("script target cannot be a symbolic link")
        deleted = False
        if target.is_file():
            target.unlink()
            deleted = True
        manifest = self._load_manifest(layer)
        if name in manifest["scripts"]:
            manifest["scripts"].pop(name, None)
            self._save_manifest(layer, manifest)
            deleted = True
        return deleted

    def install_python_packages(self, layer: str, packages: list[str], *,
                                timeout_seconds: float = 180.0,
                                pip_runner: Any | None = None) -> dict[str, Any]:
        layer_dir = self._ensure_layer(layer)
        if not isinstance(packages, list) or not packages or len(packages) > 20:
            raise SharedToolsError("packages must contain 1 to 20 valid package specs")
        cleaned: list[str] = []
        for raw in packages:
            spec = str(raw).strip() if isinstance(raw, str) else ""
            if not spec or not PACKAGE_SPEC_PATTERN.fullmatch(spec):
                raise SharedToolsError(f"invalid Python package specification: {raw!r}")
            cleaned.append(spec)
        python_dir = layer_dir / "python"
        command = [
            sys.executable, "-m", "pip", "install", "--no-input", "--upgrade",
            "--target", str(python_dir), *cleaned,
        ]
        if pip_runner is not None:
            completed = pip_runner(command, timeout_seconds)
        else:
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False,
                timeout=timeout_seconds, shell=False,
            )
        stdout = (completed.stdout or "")[-2000:]
        stderr = (completed.stderr or "")[-2000:]
        if completed.returncode != 0:
            detail = (stderr or stdout or "pip install failed").strip()[:600]
            raise RuntimeError(f"pip install failed (exit {completed.returncode}): {detail}")
        now = _now()
        manifest = self._load_manifest(layer)
        existing = {item.get("spec"): item for item in manifest["packages"] if isinstance(item, dict) and item.get("spec")}
        for spec in cleaned:
            existing[spec] = {"spec": spec, "installed_at": now}
        manifest["packages"] = sorted(existing.values(), key=lambda item: item["spec"])
        self._save_manifest(layer, manifest)
        return {
            "layer": layer,
            "packages": cleaned,
            "python_path": str(python_dir),
            "installed_at": now,
            "output_tail": (stdout or stderr).strip()[-800:],
        }

    @staticmethod
    def _secure_dir(path: Path) -> None:
        if os.name != "nt":
            path.chmod(0o750)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
