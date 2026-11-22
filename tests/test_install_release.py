from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace


def _load_install_release():
    path = Path(__file__).resolve().parent.parent / "scripts" / "install-release.py"
    spec = importlib.util.spec_from_file_location("install_release_mod", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class InstallReleaseConfigTests(unittest.TestCase):
    def test_resolve_deploy_target_supports_explicit_user_and_sudo_user_and_rejects_root(self) -> None:
        mod = _load_install_release()

        def fake_pw(name: str):
            return SimpleNamespace(pw_name=name, pw_gid=1001, pw_dir=f"/home/{name}")

        def fake_gr(gid: int):
            return SimpleNamespace(gr_name="companygrp")

        resolved = mod.resolve_deploy_target(
            user="alice",
            env={},
            passwd_lookup=fake_pw,
            group_lookup=fake_gr,
            legacy_config_exists=False,
        )
        self.assertEqual(resolved["user"], "alice")
        self.assertEqual(resolved["group"], "companygrp")
        self.assertEqual(resolved["home"], PurePosixPath("/home/alice"))
        self.assertEqual(resolved["deploy"], PurePosixPath("/home/alice/company-agent-deploy"))
        self.assertEqual(resolved["data"], PurePosixPath("/home/alice/.local/share/company-agent"))
        self.assertEqual(resolved["config"], PurePosixPath("/home/alice/.config/company-agent/runtime.env"))

        sudo_resolved = mod.resolve_deploy_target(
            env={"SUDO_USER": "ubuntu"},
            passwd_lookup=fake_pw,
            group_lookup=fake_gr,
            legacy_config_exists=False,
        )
        self.assertEqual(sudo_resolved["user"], "ubuntu")

        with self.assertRaises(ValueError):
            mod.resolve_deploy_target(
                user="root",
                env={},
                passwd_lookup=fake_pw,
                group_lookup=fake_gr,
                legacy_config_exists=False,
            )

        with self.assertRaises(ValueError):
            mod.resolve_deploy_target(
                user="alice",
                data_dir="/tmp/bad-data",
                env={},
                passwd_lookup=fake_pw,
                group_lookup=fake_gr,
                legacy_config_exists=False,
            )

    def test_render_systemd_unit_replaces_legacy_user_and_paths_cleanly(self) -> None:
        mod = _load_install_release()
        tpl = (
            "[Service]\n"
            "User=lan_z\n"
            "Group=lan_z\n"
            "Environment=HOME=/home/lan_z\n"
            "WorkingDirectory=/home/lan_z/company-agent-deploy/current\n"
            "ExecStart=/home/lan_z/.local/share/company-agent/venv/bin/python /home/lan_z/company-agent-deploy/current/run-release.py admin\n"
        )
        rendered = mod.render_systemd_unit(
            tpl,
            user="opsbot",
            group="opsgrp",
            home=PurePosixPath("/srv/opsbot"),
            deploy=PurePosixPath("/opt/ca-deploy"),
            data=PurePosixPath("/var/lib/company-agent"),
            config=PurePosixPath("/etc/company-agent/runtime.env"),
        )
        self.assertIn("User=opsbot", rendered)
        self.assertIn("Group=opsgrp", rendered)
        self.assertIn("Environment=HOME=/srv/opsbot", rendered)
        self.assertIn("Environment=COMPANY_AGENT_DATA_DIR=/var/lib/company-agent", rendered)
        self.assertIn("Environment=COMPANY_AGENT_CONFIG_PATH=/etc/company-agent/runtime.env", rendered)
        self.assertIn("WorkingDirectory=/opt/ca-deploy/current", rendered)
        self.assertIn("ExecStart=/var/lib/company-agent/venv/bin/python /opt/ca-deploy/current/run-release.py admin", rendered)
        self.assertNotIn("lan_z", rendered)

    def test_wecom_service_unit_renders_deploy_user_and_wecom_role(self) -> None:
        install_release = _load_install_release()
        unit_path = Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "company-agent-wecom.service"
        rendered = install_release.render_systemd_unit(
            unit_path.read_text(encoding="utf-8"),
            user="corpbot",
            group="corpbot",
            home=PurePosixPath("/home/corpbot"),
            deploy=PurePosixPath("/opt/ca-deploy"),
            data=PurePosixPath("/var/lib/company-agent"),
            config=PurePosixPath("/etc/company-agent/runtime.env"),
        )
        self.assertIn("User=corpbot", rendered)
        self.assertIn("Group=corpbot", rendered)
        self.assertIn("Environment=COMPANY_AGENT_DATA_DIR=/var/lib/company-agent", rendered)
        self.assertIn("Environment=COMPANY_AGENT_CONFIG_PATH=/etc/company-agent/runtime.env", rendered)
        self.assertIn("WorkingDirectory=/opt/ca-deploy/current", rendered)
        self.assertIn("ExecStart=/var/lib/company-agent/venv/bin/python /opt/ca-deploy/current/run-release.py wecom", rendered)
        self.assertIn("WantedBy=company-agent.target", rendered)
        self.assertNotIn("lan_z", rendered)

    def test_target_does_not_force_start_unconfigured_optional_channels(self) -> None:
        root = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        target_text = (root / "company-agent.target").read_text(encoding="utf-8")
        self.assertNotIn("company-agent-qq.service", target_text)
        self.assertNotIn("company-agent-wecom.service", target_text)
        self.assertIn("WantedBy=company-agent.target", (root / "company-agent-qq.service").read_text(encoding="utf-8"))
        self.assertIn("WantedBy=company-agent.target", (root / "company-agent-wecom.service").read_text(encoding="utf-8"))
