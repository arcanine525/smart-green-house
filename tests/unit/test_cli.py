"""Scaffold failures must be explicit; setup must not modify source inputs."""

from pathlib import Path
import subprocess
import sys

import pytest

from greenhouse.cli import main, setup_environment
from greenhouse.settings import Settings


@pytest.fixture
def environment(tmp_path):
    for name in ("import", "raw", "output"):
        (tmp_path / name).mkdir()
    return {
        "PGHOST": "db", "PGDATABASE": "test", "PGUSER": "test",
        "PGPASSWORD": "must-not-appear-in-output",
        "GREENHOUSE_IMPORT_DIR": str(tmp_path / "import"),
        "GREENHOUSE_RAW_DIR": str(tmp_path / "raw"),
        "GREENHOUSE_OUTPUT_DIR": str(tmp_path / "output"),
    }


def test_stream_requires_explicit_identity(monkeypatch):
    from greenhouse.sensor.stream_cli import stream_command
    for key in ('GREENHOUSE_STREAM_ID', 'GREENHOUSE_CHECKPOINT_GENERATION',
                'GREENHOUSE_REPLAY_ID', 'GREENHOUSE_REPLAY_SCOPE_ID',
                'GREENHOUSE_REPLAY_CYCLE_ID', 'GREENHOUSE_REPLAY_COMPARTMENT_ID',
                'GREENHOUSE_REPLAY_QUALITY_POLICY'):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit) as error:
        stream_command([])
    assert error.value.code == 2


@pytest.mark.parametrize("changes", [
    {"PGPASSWORD": ""}, {"PGHOST": ""}, {"PGPORT": "not-a-port"},
    {"PGPORT": "0"}, {"PGPORT": "65536"}, {"GREENHOUSE_RAW_DIR": "relative"},
])
def test_settings_rejects_invalid_configuration(environment, changes):
    with pytest.raises(ValueError):
        Settings.from_env(environment | changes)


def test_settings_rejects_overlapping_input_output(environment):
    environment["GREENHOUSE_OUTPUT_DIR"] = environment["GREENHOUSE_RAW_DIR"]
    with pytest.raises(ValueError, match="thư mục riêng"):
        Settings.from_env(environment)


def test_settings_repr_does_not_disclose_password(environment):
    settings = Settings.from_env(environment)
    assert environment["PGPASSWORD"] not in repr(settings)


def test_setup_rejects_missing_mount_before_database_access(environment):
    Path(environment["GREENHOUSE_IMPORT_DIR"]).rmdir()
    with pytest.raises(ValueError, match="Mount import"):
        setup_environment(Settings.from_env(environment))


def test_setup_database_failure_returns_nonzero_without_credentials(environment, monkeypatch, capsys):
    import psycopg
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    def fail(**kwargs):
        raise psycopg.OperationalError("cannot connect: " + kwargs["password"])
    monkeypatch.setattr(psycopg, "connect", fail)
    assert main(["setup"]) == 1
    output = capsys.readouterr()
    assert "OperationalError" in output.err
    assert environment["PGPASSWORD"] not in output.err
    assert not list(Path(environment["GREENHOUSE_OUTPUT_DIR"]).iterdir())


def test_setup_is_idempotent_against_real_database(environment):
    # Real database settings are inherited from Compose; only mounts are temporary.
    import os
    for name in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        if name in os.environ:
            environment[name] = os.environ[name]
    source = Path(environment["GREENHOUSE_IMPORT_DIR"]) / "source.zip"
    source.write_bytes(b"synthetic-not-an-archive")
    settings = Settings.from_env(environment)
    before = {p: p.read_bytes() for folder in (settings.import_dir, settings.raw_dir) for p in folder.iterdir()}
    assert "Setup OK" in setup_environment(settings)
    assert "Setup OK" in setup_environment(settings)
    after = {p: p.read_bytes() for folder in (settings.import_dir, settings.raw_dir) for p in folder.iterdir()}
    assert after == before
    assert not list(settings.output_dir.iterdir())


def test_transform_alias_uses_the_pipeline_entrypoint(monkeypatch):
    import greenhouse.cli as cli
    called=[]
    def pipeline(command,argv):
        called.append((command,argv))
        return 0
    monkeypatch.setattr(cli,'pipeline_command',pipeline)
    assert cli.main(['transform','--workflow-id','synthetic-water'])==0
    assert called==[('run-pipeline',['--workflow-id','synthetic-water'])]
