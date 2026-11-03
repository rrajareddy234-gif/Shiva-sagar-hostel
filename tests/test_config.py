"""The .env loader: direct runs must read .env, and real env vars must win."""

from __future__ import annotations

import os

import pytest

from core.config import Settings, load_dotenv


def test_dotenv_values_are_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("SAGAR_TEST_VALUE", raising=False)
    env = tmp_path / ".env"
    env.write_text("SAGAR_TEST_VALUE=from-file\n")
    load_dotenv(env)
    assert os.environ["SAGAR_TEST_VALUE"] == "from-file"


def test_a_real_environment_variable_wins(tmp_path, monkeypatch):
    """Docker sets these; the file must not override what is already there."""
    monkeypatch.setenv("SAGAR_TEST_VALUE", "from-environment")
    env = tmp_path / ".env"
    env.write_text("SAGAR_TEST_VALUE=from-file\n")
    load_dotenv(env)
    assert os.environ["SAGAR_TEST_VALUE"] == "from-environment"


def test_comments_and_blanks_are_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("SAGAR_TEST_VALUE", raising=False)
    env = tmp_path / ".env"
    env.write_text("# a comment\n\nSAGAR_TEST_VALUE=ok\nnot a setting\n")
    load_dotenv(env)
    assert os.environ["SAGAR_TEST_VALUE"] == "ok"


def test_quotes_are_stripped(tmp_path, monkeypatch):
    monkeypatch.delenv("SAGAR_TEST_VALUE", raising=False)
    env = tmp_path / ".env"
    env.write_text('SAGAR_TEST_VALUE="quoted"\n')
    load_dotenv(env)
    assert os.environ["SAGAR_TEST_VALUE"] == "quoted"


def test_a_missing_file_is_not_an_error(tmp_path):
    load_dotenv(tmp_path / "does-not-exist")


def test_settings_read_the_whatsapp_values(tmp_path, monkeypatch):
    """The bug this guards: a direct run ignored .env, so the webhook 403'd."""
    for name in ("META_VERIFY_TOKEN", "META_APP_SECRET", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "DATABASE_URL=postgresql://x/y\n"
        "META_VERIFY_TOKEN=file-verify\n"
        "META_APP_SECRET=file-secret\n"
    )
    load_dotenv(env)
    settings = Settings.from_env()
    assert settings.meta_verify_token == "file-verify"
    assert settings.channel_credentials()["verify_token"] == "file-verify"
