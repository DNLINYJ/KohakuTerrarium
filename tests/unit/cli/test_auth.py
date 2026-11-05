"""Unit tests for :mod:`kohakuterrarium.cli.auth` — login dispatch per backend."""

from kohakuterrarium.cli import auth as auth_mod
from kohakuterrarium.cli.auth import login_cli

from tests.unit._fakes.package_backend import (
    echo_backend,
    install_backend_package,
    kt_home,  # noqa: F401
)


def _no_key_prompt(*_args):
    raise AssertionError("must not prompt for an API key")


class TestPackageBackendLogin:
    def test_own_login_backend_refuses_without_prompting(
        self, kt_home, monkeypatch, capsys
    ):
        install_backend_package(
            kt_home, "pkg-x", [echo_backend(credentials="external")]
        )
        monkeypatch.setattr(auth_mod, "login_with_api_key", _no_key_prompt)
        assert login_cli("echo") == 1
        out = capsys.readouterr().out
        assert "pkg-x" in out and "own login" in out

    def test_api_key_package_backend_uses_the_key_flow(self, kt_home, monkeypatch):
        install_backend_package(kt_home, "pkg-x", [echo_backend()])
        calls = []
        monkeypatch.setattr(
            auth_mod, "login_with_api_key", lambda p, env: calls.append(p) or 0
        )
        assert login_cli("echo") == 0
        assert calls == ["echo"]

    def test_unknown_provider(self, kt_home, capsys):
        assert login_cli("ghost") == 1
        assert "Unknown provider" in capsys.readouterr().out
