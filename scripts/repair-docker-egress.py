"""Repair stale inherited Docker proxy state without resetting containers/data (root)."""
import json
import pathlib
import subprocess


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


if __name__ == "__main__":
    config = pathlib.Path("/etc/docker/daemon.json")
    original = config.read_text() if config.exists() else None
    values = json.loads(original or "{}")
    if not values.get("live-restore"):
        if original is not None:
            backup = config.with_name("daemon.json.before-company-agent")
            if backup.exists():
                raise SystemExit("Backup already exists; inspect before changing daemon config")
            backup.write_text(original)
        values["live-restore"] = True
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(values, indent=2) + "\n")
        run("dockerd", "--validate", "--config-file", str(config))
        run("systemctl", "reload", "docker")
    assert json.loads(run("docker", "info", "--format", "{{json .LiveRestoreEnabled}}"))
    before = json.loads(run("docker", "inspect", *run("docker", "ps", "-q").split()))
    # Restart only the daemon. Its current systemd configuration has no proxy;
    # the old dockerd inherited proxy settings which have since been removed.
    run("systemctl", "restart", "docker")
    after = json.loads(run("docker", "inspect", *[c["Id"] for c in before]))
    for old, new in zip(before, after):
        assert new["State"]["Running"]
        assert old["State"]["StartedAt"] == new["State"]["StartedAt"], new["Name"]
        print("container preserved:", new["Name"], new["State"]["StartedAt"])
    print(run("docker", "info", "--format", "HTTPProxy={{.HTTPProxy}} HTTPSProxy={{.HTTPSProxy}} LiveRestore={{.LiveRestoreEnabled}}"))
