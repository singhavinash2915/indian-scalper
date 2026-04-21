"""Production entry point — component wiring + deployment artefact
sanity (Dockerfile / docker-compose / systemd unit).

We do NOT run uvicorn or boot a real scheduler here — those are smoke
concerns best verified with a live deploy. These tests lock down the
*composition*: given a Settings, do build_context and build_scheduler
return the expected shapes, and do the shipped deployment files contain
the critical directives?
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.settings import Settings
from scheduler.scan_loop import ScanContext
from serve import build_context, build_scheduler, load_or_create_config

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- #
# serve.build_context                                                   #
# --------------------------------------------------------------------- #

def test_build_context_returns_paper_broker_wiring(tmp_path: Path) -> None:
    settings = Settings.from_template()
    settings.raw["storage"]["db_path"] = str(tmp_path / "scalper.db")

    ctx, broker = build_context(settings)

    assert isinstance(ctx, ScanContext)
    assert ctx.broker is broker
    # Universe is whatever the instruments table holds right now — may be
    # empty in a fresh DB; the point is the attribute exists + is a list.
    assert isinstance(ctx.universe, list)
    # The shared StateStore must point at our tmp DB.
    assert str(broker._db_path) == str(tmp_path / "scalper.db")


def test_build_context_rejects_live_broker(tmp_path: Path) -> None:
    settings = Settings.from_template()
    settings.broker = "upstox"  # paper-only loop
    with pytest.raises(RuntimeError, match="broker: paper"):
        build_context(settings)


# --------------------------------------------------------------------- #
# serve.build_scheduler                                                 #
# --------------------------------------------------------------------- #

def test_build_scheduler_registers_scan_tick_with_configured_interval(tmp_path: Path) -> None:
    settings = Settings.from_template()
    settings.raw["storage"]["db_path"] = str(tmp_path / "scalper.db")
    settings.strategy.scan_interval_seconds = 120  # override default

    ctx, _ = build_context(settings)
    scheduler = build_scheduler(ctx)

    jobs = scheduler.get_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.id == "scan_tick"
    # IntervalTrigger exposes .interval as a timedelta.
    assert job.trigger.interval.total_seconds() == 120
    assert job.max_instances == 1
    assert job.coalesce is True
    # Scheduler is *not* started in the helper — the main loop starts it.
    assert scheduler.running is False


# --------------------------------------------------------------------- #
# load_or_create_config                                                 #
# --------------------------------------------------------------------- #

def test_load_or_create_config_materialises_template(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    assert not cfg.exists()
    out = load_or_create_config(cfg)
    assert out == cfg
    assert cfg.exists()
    # Loading it back should yield a valid Settings.
    settings = Settings.load(cfg)
    assert settings.broker == "paper"
    assert settings.mode == "paper"


def test_load_or_create_config_preserves_existing_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("mode: paper\nbroker: paper\n")  # minimal valid (edit won't load)
    before = cfg.read_text()
    load_or_create_config(cfg)
    # Must NOT overwrite — operator's edits are sacred.
    assert cfg.read_text() == before


# --------------------------------------------------------------------- #
# Dockerfile structural checks                                          #
# --------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return (REPO_ROOT / "Dockerfile").read_text()


def test_dockerfile_is_multistage(dockerfile_text: str) -> None:
    assert "AS builder" in dockerfile_text
    assert "AS runtime" in dockerfile_text


def test_dockerfile_runs_as_non_root(dockerfile_text: str) -> None:
    assert "useradd" in dockerfile_text
    assert "USER scalper" in dockerfile_text


def test_dockerfile_exposes_dashboard_port(dockerfile_text: str) -> None:
    assert "EXPOSE 8080" in dockerfile_text


def test_dockerfile_has_healthcheck_hitting_health_endpoint(dockerfile_text: str) -> None:
    assert "HEALTHCHECK" in dockerfile_text
    assert "/health" in dockerfile_text


def test_dockerfile_uses_uv_and_frozen_lockfile(dockerfile_text: str) -> None:
    assert "uv sync" in dockerfile_text
    assert "--frozen" in dockerfile_text  # reproducible builds
    assert "--no-dev" in dockerfile_text  # runtime image has no pytest/ruff


def test_dockerfile_cmd_is_serve(dockerfile_text: str) -> None:
    assert 'CMD ["python", "-m", "serve"]' in dockerfile_text


# --------------------------------------------------------------------- #
# docker-compose.yml structural checks                                  #
# --------------------------------------------------------------------- #

def test_compose_binds_loopback_by_default() -> None:
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    # No auth yet — the active port mapping must bind to loopback.
    # We match the uncommented list entry (dash-prefixed) specifically
    # so that documentation comments mentioning "0.0.0.0" don't trip us.
    assert '- "127.0.0.1:8080:8080"' in text
    # Scan non-comment lines only.
    non_comment = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    assert "0.0.0.0:8080:8080" not in non_comment


def test_compose_mounts_state_volumes() -> None:
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    for line in ("./data:/app/data", "./logs:/app/logs", "./config.yaml:/app/config.yaml"):
        assert line in text


def test_compose_has_healthcheck() -> None:
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "healthcheck:" in text
    assert "/health" in text


# --------------------------------------------------------------------- #
# systemd unit structural checks                                        #
# --------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def systemd_unit() -> str:
    return (REPO_ROOT / "deploy" / "indian-scalper.service").read_text()


def test_systemd_unit_runs_as_non_root(systemd_unit: str) -> None:
    assert "User=scalper" in systemd_unit
    assert "Group=scalper" in systemd_unit


def test_systemd_unit_restart_on_failure(systemd_unit: str) -> None:
    assert "Restart=on-failure" in systemd_unit


def test_systemd_unit_has_hardening(systemd_unit: str) -> None:
    # The sandboxing block is what makes this unit safe to run on a box
    # that also does other things.
    for directive in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "PrivateTmp=yes",
        "ReadWritePaths=/opt/indian-scalper/data /opt/indian-scalper/logs",
    ):
        assert directive in systemd_unit


def test_systemd_unit_execstart_points_at_venv(systemd_unit: str) -> None:
    assert "/opt/indian-scalper/.venv/bin/python -m serve" in systemd_unit


def test_systemd_unit_after_network(systemd_unit: str) -> None:
    assert "After=network-online.target" in systemd_unit


# --------------------------------------------------------------------- #
# .dockerignore must keep the build context tight                        #
# --------------------------------------------------------------------- #

def test_dockerignore_excludes_state_and_vcs() -> None:
    text = (REPO_ROOT / ".dockerignore").read_text()
    for entry in (".venv/", "data/", "logs/", ".git/", "__pycache__/", "config.yaml", ".env"):
        assert entry in text, f"missing {entry!r} from .dockerignore"
