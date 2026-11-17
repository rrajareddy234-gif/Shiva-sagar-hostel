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
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
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


def test_start_app_restores_the_knowledge_base():
    """A lost volume must not come back with an empty knowledge base.

    The recovery path restored the newest backup, but that backup predated
    hostel_facts and faq. The app came up healthy and answered guests from
    nothing: no address, no amenities, no FAQs. The script now loads the seed
    when -- and only when -- the knowledge base is missing.
    """
    text = (SCRIPTS_ROOT / "start_app.sh").read_text()
    assert "db/seed.sql" in text, "recovery must be able to load the seed"
    assert "hostel_facts" in text, "recovery must check the knowledge base"


def test_start_app_does_not_reset_the_owners_confirmation_flags():
    """The seed must not run on every start.

    db/seed.sql sets rooms_confirmed = false. Running it on each restart would
    silently undo the owner's own confirmation of the room list.
    """
    text = (SCRIPTS_ROOT / "start_app.sh").read_text()
    assert "db/seed.sql" in text, "expected the seed to be loaded somewhere"
    guard_at = text.index("facts=")
    seed_at = text.index("db/seed.sql")
    assert guard_at < seed_at, "the seed load must sit behind the empty check"


def test_start_app_can_restore_the_backups_that_are_actually_written():
    """The recovery path must find the dumps the backup job produces.

    `backup.sh` writes `*.sql.gz`. The recovery path looked only for `*.sql`,
    so once the nightly job had run there was nothing to find and a lost volume
    came back as an empty hostel -- the owner's real bookings sitting unread in
    the backups directory. Both names must be searched, and a compressed dump
    decompressed on the way in.
    """
    text = (SCRIPTS_ROOT / "start_app.sh").read_text()
    assert "backups/*.sql.gz" in text, "recovery must look for the gzipped backups"
    assert "backups/*.sql" in text, "recovery must still accept an older plain dump"
    assert "gunzip" in text, "a gzipped dump must be decompressed before restore"
