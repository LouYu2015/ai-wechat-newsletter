"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_git_config(tmp_path_factory):
    """Isolate ``git`` subprocesses from the host's global / system config.

    Tests that spawn ``git`` (e.g. ``test_publisher``) otherwise inherit the
    developer's global settings — commit signing, custom ``gpg.ssh.program``,
    http proxies, ``init.defaultBranch``, etc. In sandboxed environments
    (Claude Code, CI containers) those settings can break throwaway test
    repos that the host's signing service / proxy has no authority over.

    The fixture points ``GIT_CONFIG_GLOBAL`` at an empty file and sets
    ``GIT_CONFIG_NOSYSTEM=1`` so ``/etc/gitconfig`` is also ignored. ``HOME``
    is redirected to a temp dir as a belt-and-suspenders fallback for any
    code path that reads ``~/.gitconfig`` directly. All overrides are
    restored when the session ends.

    Tests that create a temp repo still need to set ``user.email`` /
    ``user.name`` and an explicit initial branch — those are intrinsic
    test requirements, not sandbox workarounds.
    """
    isolated = tmp_path_factory.mktemp("git-isolated")
    empty_global = isolated / "gitconfig"
    empty_global.touch()

    mp = pytest.MonkeyPatch()
    mp.setenv("GIT_CONFIG_GLOBAL", str(empty_global))
    mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
    mp.setenv("HOME", str(isolated))
    try:
        yield
    finally:
        mp.undo()
