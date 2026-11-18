"""What the deployment has to keep true.

Upstream Sellbird runs on the operator's own machine: host networking, the
checkout bind-mounted at /app, no published port. This fork is deployed to a
container host behind a reverse proxy, so that shape does not apply and the
tests that asserted it were red for months, which teaches everyone to ignore
the suite. What has to hold here instead:

* everything stateful is on one named volume, so a redeploy keeps it;
* nothing stateful is a single-file mount, which would break os.replace;
* the Claude login is inside that same volume rather than a second one;
* a public hostname never ships without a sign-in to go with it;
* no env_file snapshot, or keys typed into the dashboard would be masked by
  the environment the container was created with.
"""
from pathlib import Path

import yaml

from sellbird.paths import PROJECT_ROOT


def service():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())["services"]["sellbird"]


def compose():
    return yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text())


def mounts():
    """Every volume line as (source, target)."""
    out = []
    for mount in service()["volumes"]:
        if isinstance(mount, str):
            parts = mount.split(":")
            out.append((parts[0], parts[1] if len(parts) > 1 else ""))
        else:
            out.append((mount.get("source", ""), mount.get("target", "")))
    return out


def test_everything_stateful_is_on_one_named_volume():
    """The database, .env, the trained config, skills, prompts and exports all
    live under /config. A redeploy replaces the container, not the volume."""
    targets = [target for _, target in mounts()]
    assert "/config" in targets
    source = next(src for src, target in mounts() if target == "/config")
    assert source in compose()["volumes"], f"{source} is mounted but never declared"


def test_nothing_stateful_is_a_single_file_mount():
    """A file mounted on its own cannot be replaced atomically, and every
    save Sellbird makes is a write-then-replace."""
    assert not [target for _, target in mounts() if target.startswith("/config/")]


def test_the_claude_login_is_on_that_same_volume():
    """It used to have a volume of its own while the image pointed
    CLAUDE_CONFIG_DIR somewhere else, so one of the two was always a lie."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "CLAUDE_CONFIG_DIR=/config/.claude" in dockerfile
    assert "/root/.claude" not in (PROJECT_ROOT / "docker-compose.yml").read_text()


def test_the_published_port_is_the_one_the_dashboard_listens_on():
    published = service()["ports"]
    assert any("5555" in str(rule) for rule in published)


def test_a_public_hostname_never_ships_without_a_sign_in():
    """SELLBIRD_CLOUD_HOST makes the dashboard listen on every interface. A
    compose file that set it without a password would publish the pipeline."""
    env = service()["environment"]
    assert "SELLBIRD_CLOUD_HOST" in env
    assert "DASHBOARD_USER" in env
    assert {"DASHBOARD_PASS", "DASHBOARD_PASS_HASH"} & set(env)


def test_the_hashed_password_is_the_one_offered_first():
    """Both are read, but the file should push people at the hash: a plain
    password sits readable in the host's configuration forever."""
    text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert text.index("DASHBOARD_PASS_HASH:") < text.index("DASHBOARD_PASS:")
    assert "password-hash" in text


def test_no_environment_snapshot_masks_the_dashboard_settings():
    # Keys typed into Settings are written to /config/.env and read at
    # startup; an env_file captured at create time would win over them.
    assert "env_file" not in service()


def test_setup_and_atomic_training_saves_survive_reloading(tmp_path, monkeypatch):
    from sellbird import setup, dashboard
    from sellbird.config import load_config
    from sellbird.trainer import write_trained_config

    base = tmp_path / "sellbird.yaml"
    base.write_text((PROJECT_ROOT / "sellbird.yaml").read_text())
    env = tmp_path / ".env"
    env.write_text("SERPER_API_KEY=fixture_original\n")
    monkeypatch.setattr(setup, "CONFIG_FILE", base)
    monkeypatch.setattr(setup, "ENV_FILE", env)
    monkeypatch.setattr(dashboard, "ENV_FILE", env)
    wizard = setup.SetupWizard()
    wizard.env_vars = {"SERPER_API_KEY": "fixture_setup"}
    wizard.config = {"usage": {"max_daily_claude_percent": 7}}
    wizard._write_env()
    wizard._write_config()
    assert wizard.config_saved
    dashboard._write_env_file({"SERPER_API_KEY": "fixture_dashboard"})
    learned = yaml.safe_load(base.read_text())
    learned["product"]["name"] = "Fixture Product"
    local = tmp_path / "sellbird.local.yaml"
    write_trained_config(learned, local)
    learned["product"]["name"] = "Fixture Updated"
    write_trained_config(learned, local)
    assert load_config(str(local)).product.name == "Fixture Updated"
    assert load_config(str(local)).usage.max_daily_claude_percent == 7
    assert "SERPER_API_KEY=fixture_dashboard" in env.read_text()
