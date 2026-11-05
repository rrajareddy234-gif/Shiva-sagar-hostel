"""Guards on the operational shell scripts.

These are not unit tests of shell logic; they are cheap checks that catch the
mistakes that actually cost data. The one that motivated this file: `backup.sh`
piped a decompressed dump into `grep -q`. Under `set -o pipefail`, grep exiting
early sends SIGPIPE to the producer, the pipeline reports failure, and the
script deleted a perfectly good backup. Nothing in the Python suite would ever
have noticed.
"""

import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))


def test_there_are_scripts_to_check():
    assert SCRIPTS, "no deploy/*.sh scripts found; the path is wrong"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_is_executable(script):
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_pipe_into_grep_under_pipefail(script):
    """A pipeline ending in `grep -q` is unsafe with `set -o pipefail`.

    grep stops at the first match, the producer gets SIGPIPE, and the whole
    pipeline is reported as failed — the opposite of the intent. Decompress or
    list to a file, then grep the file.
    """
    text = script.read_text()
    if "pipefail" not in text:
        pytest.skip("script does not set pipefail")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if "|" in line and "grep -q" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, f"{script.name} pipes into grep -q: {offenders}"


def test_backup_verifies_before_declaring_success():
    text = (DEPLOY / "backup.sh").read_text()
    # The script must test the dump is non-empty and readable, and must not
    # remove the file except when it is genuinely bad.
    assert "-s \"$FILE\"" in text or "-s \"${FILE}\"" in text
    assert "gzip -t" in text


def test_restore_drill_never_targets_the_live_database():
    """The drill must restore into a scratch database, not production."""
    text = (DEPLOY / "restore_drill.sh").read_text()
    assert "SCRATCH" in text
    assert "_drill" in text
