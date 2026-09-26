"""Exercita falhas e limpeza dos scripts sem tocar em um banco real."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("sh")
if not SHELL and Path("C:/Program Files/Git/bin/bash.exe").exists():
    SHELL = "C:/Program Files/Git/bin/bash.exe"

pytestmark = pytest.mark.skipif(not SHELL, reason="Shell POSIX não disponível")


@pytest.fixture
def shell_project(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("backup.sh", "restore.sh"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        '#!/usr/bin/env sh\n'
        'printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        'case "$*" in\n'
        '  *pg_dump*) printf "fake dump"; exit "${FAIL_DUMP:-0}" ;;\n'
        '  *pg_restore*) cat >/dev/null; exit "${FAIL_RESTORE:-0}" ;;\n'
        '  *SELECT*) printf "0001\\n" ;;\n'
        'esac\n',
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)
    return tmp_path


def run_script(project, script, *args, **env):
    bin_path = (project / "bin").as_posix()
    if os.name == "nt":
        bin_path = f"/{bin_path[0].lower()}{bin_path[2:]}"
    return subprocess.run(
        [
            SHELL, "-c", 'export PATH="$1:$PATH"; shift; exec sh "$@"',
            "sh", bin_path,
            (project / "scripts" / script).as_posix(), *args,
        ],
        env={**os.environ, "COMMAND_LOG": (project / "commands.log").as_posix(), **env},
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_backup_retains_only_its_14_latest_dumps(shell_project):
    backups = shell_project / "backups"
    backups.mkdir()
    for number in range(16):
        (backups / f"backup_20000101T000000Z_{number:06d}.dump").write_text("old")
    unrelated = backups / "manual backup.dump"
    unrelated.write_text("keep")
    result = run_script(shell_project, "backup.sh")
    assert result.returncode == 0, result.stderr
    assert len(list(backups.glob("backup_*.dump"))) == 14
    assert unrelated.read_text() == "keep"
    assert not (backups / ".backup.lock").exists()


def test_failed_backup_removes_partial_and_lock(shell_project):
    result = run_script(shell_project, "backup.sh", FAIL_DUMP="1")
    assert result.returncode != 0
    assert list((shell_project / "backups").iterdir()) == []


@pytest.mark.parametrize("fail_restore", ["0", "1"])
def test_restore_always_cleans_up_its_temporary_database(shell_project, fail_restore):
    (shell_project / "sample.dump").write_text("dump")
    result = run_script(shell_project, "restore.sh", "sample.dump", FAIL_RESTORE=fail_restore)
    assert result.returncode == int(fail_restore), result.stderr
    commands = (shell_project / "commands.log").read_text().splitlines()
    create = next(line for line in commands if "createdb" in line)
    drop = next(line for line in commands if "dropdb" in line)
    target = create.split()[-1]
    assert target.startswith("pi_restore_")
    assert drop.split()[-1] == target
    assert not any("stop api" in line for line in commands)
