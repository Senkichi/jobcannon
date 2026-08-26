"""Fork safety for the PostHog seam (#129): a forked worker must abandon the
master's inherited PostHog client and build its own, mirroring
jobcannon.db.pool's after_in_child hook (see tests/host/test_pool_fork_safety.py
and jobcannon/db/pool.py's "Fork safety" block) — see jobcannon/host/wiring.py
for the full root-cause writeup (posthog.Posthog spawns a background Consumer
daemon thread at construction time; threads do not survive fork()).

Unlike the DB pool, there is no shared-socket hazard for PostHog (HTTP batches
per request, not a held connection), so there is nothing to stash in an
orphan list — the hook just replaces the client. _reinit_posthog_after_fork's
BODY is exercised directly (os.register_at_fork doesn't exist on Windows, and
actually forking is a POSIX-only concern); the registration WIRING is covered
separately below. Construction-level only: fakes, no live Postgres, no real
PostHog network calls — pool_mod.open_pool/close_pool are monkeypatched to
no-ops wherever init_engine_seams itself is invoked, so these tests never
need POSTGRES_ADMIN_DSN.
"""

from __future__ import annotations

from jobcannon.host import posthog_client, wiring
from jobcannon.host.config import HostConfig


def _reset_hook_state(monkeypatch):
    """Isolate the module-level fork-hook state each test owns, the same
    role pool.py's test file's _isolate() plays for the DB pool globals."""
    monkeypatch.setattr(wiring, "_POSTHOG_FORK_HOOK_INSTALLED", False)
    monkeypatch.setattr(wiring, "_current_host_config", None)


def test_install_posthog_fork_hook_wires_reinit_as_after_in_child(monkeypatch):
    """The wiring half: registration must bind the real
    _reinit_posthog_after_fork, not merely happen. Faked on both platforms
    so the assertion runs on Windows dev too."""
    _reset_hook_state(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        wiring.os,
        "register_at_fork",
        lambda **kwargs: calls.append(kwargs),
        raising=False,
    )
    assert wiring._install_posthog_fork_hook() is True
    assert calls == [{"after_in_child": wiring._reinit_posthog_after_fork}]


def test_install_posthog_fork_hook_declines_without_register_at_fork(monkeypatch):
    _reset_hook_state(monkeypatch)
    monkeypatch.delattr(wiring.os, "register_at_fork", raising=False)
    assert wiring._install_posthog_fork_hook() is False
    assert wiring._POSTHOG_FORK_HOOK_INSTALLED is False


def test_install_posthog_fork_hook_is_one_time_per_process(monkeypatch):
    """os.register_at_fork registrations are permanent and accumulate;
    calling the installer twice must register the callback only once."""
    _reset_hook_state(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        wiring.os,
        "register_at_fork",
        lambda **kwargs: calls.append(kwargs),
        raising=False,
    )
    assert wiring._install_posthog_fork_hook() is True
    assert wiring._install_posthog_fork_hook() is False
    assert calls == [{"after_in_child": wiring._reinit_posthog_after_fork}]


def test_init_engine_seams_registers_fork_hook_at_most_once(monkeypatch):
    """The actual brief requirement, at the integration point: init_engine_seams
    (and therefore create_app) is called repeatedly across a process's life —
    real re-init paths, and every test in this suite that wires seams — so
    calling it twice must still leave exactly one after_in_child registration.

    pool_mod.open_pool/close_pool are stubbed so this never touches Postgres;
    every other seam call in init_engine_seams/teardown_engine_seams is cheap,
    real, in-process global reassignment.
    """
    _reset_hook_state(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        wiring.os,
        "register_at_fork",
        lambda **kwargs: calls.append(kwargs),
        raising=False,
    )
    monkeypatch.setattr(wiring.pool_mod, "open_pool", lambda *a, **kw: None)
    monkeypatch.setattr(wiring.pool_mod, "close_pool", lambda *a, **kw: None)

    cfg = HostConfig(database_url="postgresql://u:p@192.0.2.9/db")
    try:
        wiring.init_engine_seams(cfg)
        wiring.init_engine_seams(cfg)
    finally:
        wiring.teardown_engine_seams()

    assert calls == [{"after_in_child": wiring._reinit_posthog_after_fork}]


def test_reinit_posthog_after_fork_rebuilds_from_current_config(monkeypatch):
    """The hook must read the CURRENT stashed host_config at fork time, not
    a value captured when the hook was registered — init_engine_seams can be
    called again (updating the stash) after registration already happened."""
    _reset_hook_state(monkeypatch)
    built_with: list = []
    set_calls: list = []
    monkeypatch.setattr(
        wiring,
        "_build_posthog_client",
        lambda host_config: (
            built_with.append(host_config) or f"client-for-{host_config.posthog_api_key}"
        ),
    )
    monkeypatch.setattr(posthog_client, "set_posthog_client", set_calls.append)

    cfg_a = HostConfig(database_url="x", posthog_api_key="key-a")
    monkeypatch.setattr(wiring, "_current_host_config", cfg_a)
    wiring._reinit_posthog_after_fork()

    cfg_b = HostConfig(database_url="x", posthog_api_key="key-b")
    monkeypatch.setattr(wiring, "_current_host_config", cfg_b)
    wiring._reinit_posthog_after_fork()

    assert built_with == [cfg_a, cfg_b]
    assert set_calls == ["client-for-key-a", "client-for-key-b"]


def test_reinit_posthog_after_fork_installs_none_without_api_key(monkeypatch, caplog):
    """_build_posthog_client(host_config) returns None when no PostHog API
    key is configured — the hook must still install that None (mirroring
    init_engine_seams' own inert-seam behavior), not skip the call. The log
    line must say "absent", not the "rebuilt" line reserved for a real
    client, so log-signature watchers can't mistake a missing key for a
    healthy post-fork rebuild."""
    _reset_hook_state(monkeypatch)
    set_calls: list = []
    monkeypatch.setattr(posthog_client, "set_posthog_client", set_calls.append)
    monkeypatch.setattr(wiring, "_current_host_config", HostConfig(database_url="x"))

    with caplog.at_level("INFO"):
        wiring._reinit_posthog_after_fork()

    assert set_calls == [None]
    assert "posthog client absent after fork" in caplog.text
    assert "posthog client rebuilt after fork" not in caplog.text


def test_reinit_posthog_after_fork_noops_without_current_config(monkeypatch):
    """A fork with no host_config ever wired (e.g. before the first
    init_engine_seams call) must be a clean no-op, not an AttributeError
    swallowed by the broad except."""
    _reset_hook_state(monkeypatch)
    set_calls: list = []
    monkeypatch.setattr(posthog_client, "set_posthog_client", set_calls.append)

    wiring._reinit_posthog_after_fork()

    assert set_calls == []


def test_reinit_posthog_after_fork_swallows_rebuild_exceptions(monkeypatch):
    """Must never raise: this runs inside fork machinery."""
    _reset_hook_state(monkeypatch)

    def boom(host_config):
        raise RuntimeError("posthog client rebuild exploded")

    set_calls: list = []
    monkeypatch.setattr(wiring, "_build_posthog_client", boom)
    monkeypatch.setattr(posthog_client, "set_posthog_client", set_calls.append)
    monkeypatch.setattr(wiring, "_current_host_config", HostConfig(database_url="x"))

    wiring._reinit_posthog_after_fork()  # must not raise

    assert set_calls == []


def test_teardown_clears_current_config_but_not_install_flag(monkeypatch):
    """teardown_engine_seams must clear the stashed config (so a fork racing
    a teardown rebuilds against 'no config' rather than resurrecting a client
    teardown deliberately removed) but must NOT reset the one-time install
    flag (that flag tracks a permanent OS-level registration, not the
    current wiring state — resetting it would double-register on the next
    init_engine_seams call)."""
    _reset_hook_state(monkeypatch)
    monkeypatch.setattr(wiring.pool_mod, "open_pool", lambda *a, **kw: None)
    monkeypatch.setattr(wiring.pool_mod, "close_pool", lambda *a, **kw: None)
    monkeypatch.setattr(wiring.os, "register_at_fork", lambda **kwargs: None, raising=False)

    wiring.init_engine_seams(HostConfig(database_url="x"))
    assert wiring._current_host_config is not None
    assert wiring._POSTHOG_FORK_HOOK_INSTALLED is True

    wiring.teardown_engine_seams()

    assert wiring._current_host_config is None
    assert wiring._POSTHOG_FORK_HOOK_INSTALLED is True
