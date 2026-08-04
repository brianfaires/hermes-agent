"""Regression coverage for cross-profile durable-session recovery aliases."""

import json

import pytest

import hermes_state
from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _source(profile: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="8244556262",
        chat_name="Brian",
        chat_type="dm",
        user_id="8244556262",
        profile=profile,
    )


def _store(tmp_path, monkeypatch) -> SessionStore:
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    config.multiplex_profiles = True
    return SessionStore(sessions_dir=tmp_path / "sessions", config=config)


def _give_activity(store: SessionStore, session_id: str) -> None:
    store._db.append_message(
        session_id=session_id,
        role="user",
        content="profile isolation fixture",
    )


def test_same_peer_on_distinct_profiles_never_recovers_foreign_session(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    ang = store.get_or_create_session(_source("ang"))
    _give_activity(store, ang.session_id)

    default = store.get_or_create_session(_source("default"))

    assert default.session_id != ang.session_id
    assert store._db.get_session(ang.session_id)["profile_name"] == "ang"
    assert store._db.get_session(default.session_id)["profile_name"] == "default"


def test_missing_source_profile_persists_resolved_owner_and_cannot_fall_to_default(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "ang")
    source_without_profile = _source("ang")
    source_without_profile.profile = None
    ang = store.get_or_create_session(source_without_profile)
    _give_activity(store, ang.session_id)

    row = store._db.get_session(ang.session_id)
    assert row["profile_name"] == "ang"

    store._db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET session_key = NULL WHERE id = ?",
            (ang.session_id,),
        )
    )
    with store._lock:
        store._entries.clear()

    default = store.get_or_create_session(_source("default"))

    assert default.session_id != ang.session_id


def test_same_profile_peer_fallback_still_recovers_when_exact_key_is_missing(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source("ang")
    original = store.get_or_create_session(source)
    _give_activity(store, original.session_id)
    store._db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET session_key = NULL WHERE id = ?",
            (original.session_id,),
        )
    )
    with store._lock:
        store._entries.clear()

    recovered = store.get_or_create_session(source)

    assert recovered.session_id == original.session_id


def test_rightful_owner_recovers_row_with_stale_foreign_key(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source("ang")
    original = store.get_or_create_session(source)
    _give_activity(store, original.session_id)
    foreign_key = store._generate_session_key(_source("default"))
    store._db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET session_key = ? WHERE id = ?",
            (foreign_key, original.session_id),
        )
    )
    with store._lock:
        store._entries.clear()

    recovered = store.get_or_create_session(source)

    assert recovered.session_id == original.session_id


def test_legacy_named_profile_recovers_from_exact_key(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source("ang")
    original = store.get_or_create_session(source)
    _give_activity(store, original.session_id)
    store._db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET profile_name = NULL WHERE id = ?",
            (original.session_id,),
        )
    )
    with store._lock:
        store._entries.clear()

    recovered = store.get_or_create_session(source)

    assert recovered.session_id == original.session_id


def test_exact_key_row_with_foreign_profile_is_rejected(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    ang = store.get_or_create_session(_source("ang"))
    _give_activity(store, ang.session_id)
    main_key = store._generate_session_key(_source("default"))
    store._db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET session_key = ? WHERE id = ?",
            (main_key, ang.session_id),
        )
    )

    recovered = store._recover_session_from_db(
        session_key=main_key,
        source=_source("default"),
        now=ang.updated_at,
    )

    assert recovered is None


def test_routing_load_drops_foreign_alias_and_keeps_owner_route(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    ang_source = _source("ang")
    ang = store.get_or_create_session(ang_source)
    _give_activity(store, ang.session_id)
    ang_key = store._generate_session_key(ang_source)
    main_key = store._generate_session_key(_source("default"))
    owner_json = store._db.load_gateway_routing_entries(scope=store._routing_scope())[ang_key]
    store._db.save_gateway_routing_entry(main_key, owner_json, scope=store._routing_scope())

    reloaded = _store(tmp_path, monkeypatch)
    reloaded._ensure_loaded()

    assert ang_key in reloaded._entries
    assert main_key not in reloaded._entries
    assert reloaded._entries[ang_key].session_id == ang.session_id


def test_peer_metadata_write_cannot_reassign_session_owner(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    ang = store.get_or_create_session(_source("ang"))
    main_key = store._generate_session_key(_source("default"))

    with pytest.raises(ValueError, match="profile ownership"):
        store._db.record_gateway_session_peer(
            ang.session_id,
            source="telegram",
            user_id="8244556262",
            session_key=main_key,
            chat_id="8244556262",
            chat_type="dm",
            profile_name="default",
        )

    row = store._db.get_session(ang.session_id)
    assert row["profile_name"] == "ang"
    assert row["session_key"].startswith("agent:ang:")


def test_peer_metadata_write_rejects_key_that_disagrees_with_supplied_owner(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    ang = store.get_or_create_session(_source("ang"))
    main_key = store._generate_session_key(_source("default"))

    with pytest.raises(ValueError, match="session key profile"):
        store._db.record_gateway_session_peer(
            ang.session_id,
            source="telegram",
            user_id="8244556262",
            session_key=main_key,
            chat_id="8244556262",
            chat_type="dm",
            profile_name="ang",
        )

    row = store._db.get_session(ang.session_id)
    assert row["session_key"].startswith("agent:ang:")


def test_peer_metadata_write_cannot_bypass_owner_by_omitting_profile(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    ang = store.get_or_create_session(_source("ang"))
    main_key = store._generate_session_key(_source("default"))

    with pytest.raises(ValueError, match="profile ownership"):
        store._db.record_gateway_session_peer(
            ang.session_id,
            source="telegram",
            user_id="8244556262",
            session_key=main_key,
            chat_id="8244556262",
            chat_type="dm",
            profile_name=None,
        )

    assert store._db.get_session(ang.session_id)["session_key"].startswith("agent:ang:")


def test_switch_session_rejects_foreign_owner_before_any_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    default = store.get_or_create_session(_source("default"))
    ang = store.get_or_create_session(_source("ang"))
    _give_activity(store, default.session_id)
    _give_activity(store, ang.session_id)
    store._db.end_session(ang.session_id, "user_exit")
    main_key = store._generate_session_key(_source("default"))

    switched = store.switch_session(main_key, ang.session_id)

    assert switched is None
    assert store._entries[main_key].session_id == default.session_id
    assert store._db.get_session(default.session_id)["end_reason"] is None
    assert store._db.get_session(ang.session_id)["end_reason"] == "user_exit"
    routes = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert default.session_id in routes[main_key]


def test_switch_session_allows_same_profile_owner(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    original = store.get_or_create_session(_source("default"))
    current = store.reset_session(original.session_key)
    assert current is not None

    switched = store.switch_session(original.session_key, original.session_id)

    assert switched is not None
    assert switched.session_id == original.session_id
    assert store._db.get_session(original.session_id)["ended_at"] is None


def test_primary_routing_load_failure_aborts_without_erasing_routes(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    store.get_or_create_session(_source("default"))
    store.get_or_create_session(_source("ang"))
    scope = store._routing_scope()
    assert len(store._db.load_gateway_routing_entries(scope=scope)) == 2

    reloaded = _store(tmp_path, monkeypatch)
    real_loader = reloaded._db.load_gateway_routing_entries
    monkeypatch.setattr(
        reloaded._db,
        "load_gateway_routing_entries",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("transient routing load")),
    )

    with pytest.raises(RuntimeError, match="transient routing load"):
        reloaded._ensure_loaded()
    assert len(real_loader(scope=scope)) == 2

    monkeypatch.setattr(reloaded._db, "load_gateway_routing_entries", real_loader)
    third = _source("ops")
    third.user_id = "different-user"
    third.chat_id = "different-user"
    reloaded.get_or_create_session(third)
    assert len(real_loader(scope=scope)) == 3


def test_transient_ownership_lookup_aborts_load_without_erasing_routes(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    store.get_or_create_session(_source("default"))
    store.get_or_create_session(_source("ang"))
    scope = store._routing_scope()
    assert len(store._db.load_gateway_routing_entries(scope=scope)) == 2

    reloaded = _store(tmp_path, monkeypatch)
    real_get_session = reloaded._db.get_session
    monkeypatch.setattr(
        reloaded._db,
        "get_session",
        lambda _session_id: (_ for _ in ()).throw(RuntimeError("transient read")),
    )

    with pytest.raises(RuntimeError, match="transient read"):
        reloaded._ensure_loaded()
    assert len(reloaded._db.load_gateway_routing_entries(scope=scope)) == 2

    monkeypatch.setattr(reloaded._db, "get_session", real_get_session)
    third = _source("ops")
    third.user_id = "different-user"
    third.chat_id = "different-user"
    reloaded.get_or_create_session(third)
    assert len(reloaded._db.load_gateway_routing_entries(scope=scope)) == 3


def test_missing_db_row_rejects_route_with_foreign_serialized_key(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    ang = store.get_or_create_session(_source("ang"))
    ang_key = store._generate_session_key(_source("ang"))
    main_key = store._generate_session_key(_source("default"))
    entry = json.loads(
        store._db.load_gateway_routing_entries(scope=store._routing_scope())[ang_key]
    )
    entry["origin"]["profile"] = None
    store._db.save_gateway_routing_entry(
        main_key, json.dumps(entry), scope=store._routing_scope()
    )
    store._db.delete_gateway_routing_entries([ang_key], scope=store._routing_scope())
    store._db._execute_write(
        lambda conn: conn.execute("DELETE FROM sessions WHERE id = ?", (ang.session_id,))
    )

    reloaded = _store(tmp_path, monkeypatch)
    reloaded._ensure_loaded()

    assert main_key not in reloaded._entries


def test_missing_db_row_rejects_even_matching_serialized_route(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    default = store.get_or_create_session(_source("default"))
    main_key = store._generate_session_key(_source("default"))
    store._db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM sessions WHERE id = ?", (default.session_id,)
        )
    )

    reloaded = _store(tmp_path, monkeypatch)
    reloaded._ensure_loaded()

    assert main_key not in reloaded._entries


def test_ownerless_durable_row_cannot_claim_named_profile(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    ang = store.get_or_create_session(_source("ang"))
    ang_key = store._generate_session_key(_source("ang"))
    store._db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET profile_name = NULL, session_key = NULL WHERE id = ?",
            (ang.session_id,),
        )
    )

    reloaded = _store(tmp_path, monkeypatch)
    reloaded._ensure_loaded()

    assert ang_key not in reloaded._entries


def test_create_failure_does_not_publish_phantom_route(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source("ang")
    key = store._generate_session_key(source)

    monkeypatch.setattr(
        store._db,
        "create_session",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("create failed")),
    )

    with pytest.raises(RuntimeError, match="transition failed"):
        store.get_or_create_session(source)

    assert key not in store._entries
    assert key not in store._db.load_gateway_routing_entries(
        scope=store._routing_scope()
    )


def test_reset_durable_failure_preserves_existing_route(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source("ang")
    key = store._generate_session_key(source)
    original = store.get_or_create_session(source)

    monkeypatch.setattr(
        store._db,
        "promote_to_session_reset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("promote failed")
        ),
    )

    assert store.reset_session(key) is None
    assert store._entries[key].session_id == original.session_id
    assert store._db.get_session(original.session_id)["ended_at"] is None


def test_reset_route_save_failure_rolls_back_lifecycle(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    source = _source("ang")
    key = store._generate_session_key(source)
    original = store.get_or_create_session(source)
    real_replace = store._db.replace_gateway_routing_entries
    calls = 0

    def fail_once(entries, *, scope=""):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("routing write failed")
        return real_replace(entries, scope=scope)

    monkeypatch.setattr(store._db, "replace_gateway_routing_entries", fail_once)

    assert store.reset_session(key) is None
    assert store._entries[key].session_id == original.session_id
    assert store._db.get_session(original.session_id)["ended_at"] is None
    persisted = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert json.loads(persisted[key])["session_id"] == original.session_id


def _switch_failure_fixture(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    current_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-a",
        chat_name="Chat A",
        chat_type="dm",
        user_id="user-a",
        profile="ang",
    )
    target_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-b",
        chat_name="Chat B",
        chat_type="dm",
        user_id="user-b",
        profile="ang",
    )
    current_key = store._generate_session_key(current_source)
    target_key = store._generate_session_key(target_source)
    current = store.get_or_create_session(current_source)
    target_id = "ang-switch-target-chat-b"
    store._db.create_session(
        target_id,
        "telegram",
        user_id=target_source.user_id,
        session_key=target_key,
        chat_id=target_source.chat_id,
        chat_type=target_source.chat_type,
        profile_name="ang",
    )
    store._record_gateway_session_peer(
        target_id,
        target_key,
        target_source,
        display_name=target_source.chat_name,
        strict=True,
    )
    store._db.end_session(target_id, "user_exit")
    return store, current_key, current, target_id, store._db.get_session(target_id)


@pytest.mark.parametrize(
    "step",
    [
        "target_reopen",
        "target_peer_rewrite",
        "old_lifecycle",
        "route_publication",
    ],
)
def test_switch_durable_step_failure_rolls_back_all_sqlite_mutations(
    tmp_path, monkeypatch, step
):
    store, current_key, current, target_id, target_before = _switch_failure_fixture(
        tmp_path, monkeypatch
    )
    store._db._gateway_switch_fail_after_step = step

    assert store.switch_session(current_key, target_id) is None

    assert store._entries[current_key].session_id == current.session_id
    persisted = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert json.loads(persisted[current_key])["session_id"] == current.session_id
    assert store._db.get_session(current.session_id)["ended_at"] is None

    target_after = store._db.get_session(target_id)
    for column in (
        "source",
        "user_id",
        "session_key",
        "chat_id",
        "chat_type",
        "thread_id",
        "display_name",
        "origin_json",
    ):
        assert target_after[column] == target_before[column]
    assert target_after["ended_at"] is not None
    assert target_after["end_reason"] == "user_exit"


def test_nonmultiplex_recovery_rejects_explicit_foreign_owner_without_key(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    store.config.multiplex_profiles = False
    assert not store._recovered_row_allowed_for_active_profile(
        requested_session_key="agent:main:telegram:dm:12345",
        recovered={"id": "foreign", "profile_name": "ang", "session_key": None},
    )


def test_nonmultiplex_active_profile_owns_new_sessions_and_can_switch(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    store.config.multiplex_profiles = False
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "coder"
    )
    source = _source("default")
    source.profile = None
    current = store.get_or_create_session(source)
    assert store._db.get_session(current.session_id)["profile_name"] == "coder"

    store._db.create_session(
        "coder-nonmultiplex-target",
        "telegram",
        user_id=source.user_id,
        session_key="agent:main:telegram:dm:12345",
        chat_id=source.chat_id,
        chat_type=source.chat_type,
        profile_name="coder",
    )
    switched = store.switch_session(
        current.session_key, "coder-nonmultiplex-target"
    )
    assert switched is not None
    assert switched.session_id == "coder-nonmultiplex-target"


def test_switch_blocks_explicit_foreign_owner_when_multiplex_disabled(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    store.config.multiplex_profiles = False
    source = _source("default")
    source.profile = None
    current = store.get_or_create_session(source)
    key = current.session_key
    store._db.create_session(
        "ang-nonmultiplex-target",
        "telegram",
        user_id=source.user_id,
        session_key="agent:ang:telegram:dm:12345",
        chat_id=source.chat_id,
        chat_type=source.chat_type,
        profile_name="ang",
    )

    assert store.switch_session(key, "ang-nonmultiplex-target") is None
    assert store._entries[key].session_id == current.session_id


def test_legacy_default_route_is_rejected_when_durable_owner_is_blank(
    tmp_path, monkeypatch
):
    store = _store(tmp_path, monkeypatch)
    default = store.get_or_create_session(_source("default"))
    main_key = store._generate_session_key(_source("default"))
    store._db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET profile_name = NULL, session_key = NULL WHERE id = ?",
            (default.session_id,),
        )
    )

    reloaded = _store(tmp_path, monkeypatch)
    reloaded._ensure_loaded()

    assert main_key not in reloaded._entries


def test_reset_persists_profile_resolved_from_session_key(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "ang")
    source = _source("ang")
    source.profile = None
    original = store.get_or_create_session(source)

    reset = store.reset_session(original.session_key)

    assert reset is not None
    assert store._db.get_session(reset.session_id)["profile_name"] == "ang"
