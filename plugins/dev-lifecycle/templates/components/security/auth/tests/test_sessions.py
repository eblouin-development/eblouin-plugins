"""Exhaustive tests for auth's _sessions.py -- the opaque, server-side
session core that is this catalog's DEFAULT browser authentication path.

The crown jewel here is `SessionService.resolve`'s state machine, tested
the way `test_core.py` tests refresh rotation: every rejection path
(unknown, revoked, idle-expired, absolute-expired, deleted user) asserted
individually, every one asserted to raise the IDENTICAL exception type and
message, and the properties that justify preferring sessions over JWTs
(immediate revocation, live role reads, enforceable idle timeout) asserted
as behavior rather than left to the prose. Async tests use explicit
`@pytest.mark.asyncio` markers -- pytest-asyncio's default "strict" mode
picks them up with no extra `--asyncio-mode` flag or ini configuration."""

from __future__ import annotations

from datetime import timedelta

import pytest

# ---------------------------------------------------------------------------
# generate_session_id
# ---------------------------------------------------------------------------


def test_generate_session_id_is_url_safe_and_high_entropy(sessions_mod):
    raw = sessions_mod.generate_session_id()
    # secrets.token_urlsafe(32) -> 43 base64url chars, no padding.
    assert len(raw) >= 43
    assert set(raw) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_generate_session_id_never_collides(sessions_mod):
    ids = {sessions_mod.generate_session_id() for _ in range(500)}
    assert len(ids) == 500


def test_generate_session_id_is_opaque_not_a_jwt(sessions_mod):
    # The whole point of an opaque id: no structure to parse, no claims to
    # read. A JWT is three dot-separated base64 segments; a session id has
    # no dots at all, so there is nothing for a client to decode.
    assert "." not in sessions_mod.generate_session_id()


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persists_only_the_hash_never_the_raw_id(
    session_service, session_store, user_store, core_mod
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    stored = session_store.all_records()
    assert len(stored) == 1
    assert stored[0].session_hash == core_mod.hash_token(issued.session_id)
    # The raw id appears nowhere in the persisted row -- the same posture
    # RefreshRecord takes for refresh tokens.
    assert issued.session_id not in [f for record in stored for f in (record.session_hash, record.user_id)]


@pytest.mark.asyncio
async def test_create_sets_both_deadlines_from_the_injected_clock(
    session_service, session_store, user_store, clock
):
    user = await user_store.create("alice@example.com", "hash", ())
    await session_service.create(user)

    record = session_store.all_records()[0]
    assert record.created_at == clock.current
    assert record.last_seen_at == clock.current
    assert record.absolute_expires_at == clock.current + timedelta(hours=24)
    assert record.revoked is False


@pytest.mark.asyncio
async def test_create_stores_no_roles_snapshot(session_service, session_store, user_store):
    # Roles are resolved live on every request (see resolve), so the record
    # deliberately has no roles field to go stale. Asserted structurally so
    # a future change that denormalizes roles onto the row fails here.
    user = await user_store.create("alice@example.com", "hash", ("admin",))
    await session_service.create(user)
    assert not hasattr(session_store.all_records()[0], "roles")


@pytest.mark.asyncio
async def test_every_login_mints_a_fresh_id_the_fixation_defense(session_service, user_store):
    user = await user_store.create("alice@example.com", "hash", ())
    first = await session_service.create(user)
    second = await session_service.create(user)
    assert first.session_id != second.session_id


@pytest.mark.asyncio
async def test_create_emits_an_audit_event_when_a_sink_is_wired(
    sessions_mod, session_store, user_store, clock, event_sink
):
    service = sessions_mod.SessionService(session_store, user_store, clock, events=event_sink)
    user = await user_store.create("alice@example.com", "hash", ())
    await service.create(user)

    assert event_sink.events == [
        ("auth.session.created", {"actor": user.id, "outcome": "success"})
    ]


# ---------------------------------------------------------------------------
# IssuedSession.max_age_seconds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_age_measures_to_the_absolute_deadline_not_the_idle_one(
    session_service, user_store, clock
):
    # The cookie must OUTLIVE an idle period so the SERVER, not the
    # browser, decides a session went stale -- see max_age_seconds' own
    # docstring. 24h absolute vs 1h idle in the fixture.
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    assert issued.max_age_seconds(clock.current) == 24 * 3600


@pytest.mark.asyncio
async def test_max_age_clamps_at_zero_for_an_already_expired_session(
    session_service, user_store, clock
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    clock.advance(timedelta(hours=48))
    # Never negative -- some browsers read a negative Max-Age as a
    # session-length cookie rather than an immediate delete.
    assert issued.max_age_seconds(clock.current) == 0


# ---------------------------------------------------------------------------
# resolve -- the happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_the_principal_for_a_live_session(
    session_service, user_store, core_mod, clock
):
    user = await user_store.create("alice@example.com", "hash", ("admin", "auditor"))
    issued = await session_service.create(user)

    principal = await session_service.resolve(issued.session_id)
    assert principal.sub == user.id
    assert principal.roles == ["admin", "auditor"]
    assert principal.session_hash == core_mod.hash_token(issued.session_id)
    assert principal.created_at == clock.current
    assert principal.absolute_expires_at == clock.current + timedelta(hours=24)


@pytest.mark.asyncio
async def test_principal_is_duck_type_compatible_with_access_claims(
    session_service, user_store, token_service
):
    # The compatibility that lets ONE require_roles gate and ONE set of
    # route handlers serve both transports -- asserted, not just documented.
    user = await user_store.create("alice@example.com", "hash", ("admin",))
    issued = await session_service.create(user)
    principal = await session_service.resolve(issued.session_id)

    claims = token_service.decode_access(token_service.mint_access(user.id, ["admin"]))
    for attribute in ("sub", "roles"):
        assert hasattr(principal, attribute)
        assert getattr(principal, attribute) == getattr(claims, attribute)


# ---------------------------------------------------------------------------
# resolve -- every rejection path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_rejects_a_missing_cookie(session_service, sessions_mod):
    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve(None)


@pytest.mark.asyncio
async def test_resolve_rejects_a_blank_cookie(session_service, sessions_mod):
    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve("")


@pytest.mark.asyncio
async def test_resolve_rejects_an_unknown_id(session_service, sessions_mod):
    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve(sessions_mod.generate_session_id())


@pytest.mark.asyncio
async def test_resolve_rejects_a_revoked_session_on_the_very_next_request(
    session_service, user_store, sessions_mod
):
    # THE load-bearing test for preferring sessions over JWTs: revocation
    # takes effect immediately, with no TTL window to wait out.
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    await session_service.resolve(issued.session_id)  # live

    await session_service.revoke(issued.session_id)

    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve(issued.session_id)


@pytest.mark.asyncio
async def test_resolve_rejects_a_session_past_its_idle_deadline(
    session_service, user_store, clock, sessions_mod
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    clock.advance(timedelta(hours=1))  # exactly idle_ttl -- the boundary
    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve(issued.session_id)


@pytest.mark.asyncio
async def test_a_session_stays_live_right_up_to_the_idle_boundary(
    session_service, user_store, clock
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    clock.advance(timedelta(hours=1) - timedelta(seconds=1))
    principal = await session_service.resolve(issued.session_id)
    assert principal.sub == user.id


@pytest.mark.asyncio
async def test_activity_slides_the_idle_deadline_forward(session_service, user_store, clock):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    # Use it every 45 minutes for three hours -- well past idle_ttl in
    # total elapsed time, but never idle for a full hour at any point.
    for _ in range(4):
        clock.advance(timedelta(minutes=45))
        principal = await session_service.resolve(issued.session_id)
        assert principal.sub == user.id


@pytest.mark.asyncio
async def test_the_absolute_deadline_kills_a_continuously_used_session(
    session_service, user_store, clock, sessions_mod
):
    # The ceiling sliding expiry alone cannot provide: an attacker keeping
    # a stolen session warm still loses it at absolute_ttl.
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    start = clock.current

    # A request every 30 minutes never lets the 1-hour idle deadline
    # elapse, so only the 24-hour ceiling can end this session. 60 steps
    # covers 30 hours -- comfortably past it, so a failure here means the
    # ceiling did not fire at all, not that the loop was too short.
    for _ in range(60):
        clock.advance(timedelta(minutes=30))
        try:
            await session_service.resolve(issued.session_id)
        except sessions_mod.InvalidSession:
            break
    else:  # pragma: no cover -- only reached if the ceiling never fired
        pytest.fail("the absolute deadline never fired despite continuous use")

    # It was the ABSOLUTE deadline that killed it, not idle expiry: the
    # session survived right up to the 24-hour mark and died exactly there.
    assert clock.current - start == timedelta(hours=24)

    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve(issued.session_id)


@pytest.mark.asyncio
async def test_resolve_rejects_a_session_whose_user_was_deleted(
    session_service, session_store, user_store, sessions_mod
):
    # A JWT minted before the deletion would keep working until its exp;
    # a session dies with the account, with no cleanup job required.
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    user_store._by_id.pop(user.id)

    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve(issued.session_id)


@pytest.mark.asyncio
async def test_every_rejection_raises_the_identical_message(
    session_service, user_store, clock, sessions_mod
):
    # Same "don't leak which specific reason" posture as InvalidCredentials
    # and InvalidToken/TokenReused -- asserted on the MESSAGE, not just the
    # exception type, since the message is what reaches a log or a client.
    user = await user_store.create("alice@example.com", "hash", ())
    revoked = await session_service.create(user)
    await session_service.revoke(revoked.session_id)
    expired = await session_service.create(user)

    messages = set()
    for candidate in (None, "", sessions_mod.generate_session_id(), revoked.session_id):
        with pytest.raises(sessions_mod.InvalidSession) as excinfo:
            await session_service.resolve(candidate)
        messages.add(str(excinfo.value))

    clock.advance(timedelta(hours=2))
    with pytest.raises(sessions_mod.InvalidSession) as excinfo:
        await session_service.resolve(expired.session_id)
    messages.add(str(excinfo.value))

    assert len(messages) == 1


@pytest.mark.asyncio
async def test_invalid_session_is_an_auth_error_subclass(sessions_mod, core_mod):
    # So an app's existing AuthError exception handler catches it without
    # a new registration, exactly like CsrfValidationError.
    assert issubclass(sessions_mod.InvalidSession, core_mod.AuthError)


# ---------------------------------------------------------------------------
# resolve -- live roles, and the touch write-rate bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_revoked_role_stops_authorizing_on_the_next_request(
    session_service, user_store, core_mod
):
    # The property a JWT roles claim structurally cannot offer: roles are
    # read live from the user store on every resolve.
    user = await user_store.create("alice@example.com", "hash", ("admin",))
    issued = await session_service.create(user)
    assert (await session_service.resolve(issued.session_id)).roles == ["admin"]

    demoted = core_mod.UserRecord(
        id=user.id, email=user.email, password_hash=user.password_hash, roles=()
    )
    user_store._by_id[user.id] = demoted
    user_store._by_email[user.email] = demoted

    assert (await session_service.resolve(issued.session_id)).roles == []


@pytest.mark.asyncio
async def test_a_granted_role_takes_effect_on_the_next_request(
    session_service, user_store, core_mod
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    assert (await session_service.resolve(issued.session_id)).roles == []

    promoted = core_mod.UserRecord(
        id=user.id, email=user.email, password_hash=user.password_hash, roles=("admin",)
    )
    user_store._by_id[user.id] = promoted
    user_store._by_email[user.email] = promoted

    assert (await session_service.resolve(issued.session_id)).roles == ["admin"]


@pytest.mark.asyncio
async def test_resolve_does_not_write_within_the_touch_interval(
    session_service, session_store, user_store, clock
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    for _ in range(10):
        clock.advance(timedelta(seconds=5))
        await session_service.resolve(issued.session_id)

    # 50 seconds of traffic, all inside the 1-minute touch interval: not
    # one write. This is the bound that keeps every authenticated GET from
    # becoming a database write.
    assert session_store.touch_calls == []


@pytest.mark.asyncio
async def test_resolve_writes_once_the_touch_interval_has_elapsed(
    session_service, session_store, user_store, clock
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    clock.advance(timedelta(minutes=2))
    principal = await session_service.resolve(issued.session_id)

    assert len(session_store.touch_calls) == 1
    assert session_store.touch_calls[0] == (principal.session_hash, clock.current)
    assert principal.last_seen_at == clock.current


@pytest.mark.asyncio
async def test_the_touch_bound_can_only_expire_early_never_extend(
    session_service, session_store, user_store, clock
):
    # last_seen_at may lag real activity by up to touch_interval, which can
    # only SHORTEN the effective idle window -- it can never keep a stale
    # session alive. Asserted by confirming the stored last_seen_at is
    # never ahead of the clock.
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    for _ in range(20):
        clock.advance(timedelta(seconds=20))
        await session_service.resolve(issued.session_id)
        assert session_store.all_records()[0].last_seen_at <= clock.current


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotate_issues_a_new_id_and_kills_the_old_one(
    session_service, user_store, sessions_mod
):
    user = await user_store.create("alice@example.com", "hash", ())
    original = await session_service.create(user)

    rotated = await session_service.rotate(original.session_id)
    assert rotated.session_id != original.session_id

    assert (await session_service.resolve(rotated.session_id)).sub == user.id
    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.resolve(original.session_id)


@pytest.mark.asyncio
async def test_rotate_inherits_the_original_absolute_deadline(
    session_service, user_store, clock
):
    # THE regression test for rotation-as-renewal: if rotate recomputed the
    # ceiling from now(), periodic rotation would extend a session forever
    # and defeat absolute_ttl entirely.
    user = await user_store.create("alice@example.com", "hash", ())
    original = await session_service.create(user)
    original_deadline = original.record.absolute_expires_at

    # Half an hour -- inside the 1-hour idle window, so the session is
    # still live and rotation is legitimately available.
    clock.advance(timedelta(minutes=30))
    rotated = await session_service.rotate(original.session_id)

    assert rotated.record.absolute_expires_at == original_deadline
    assert rotated.record.created_at == original.record.created_at
    # last_seen_at IS reset -- the rotating request is itself activity.
    assert rotated.record.last_seen_at == clock.current


@pytest.mark.asyncio
async def test_rotate_cannot_resurrect_a_revoked_session(
    session_service, user_store, sessions_mod
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    await session_service.revoke(issued.session_id)

    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.rotate(issued.session_id)


@pytest.mark.asyncio
async def test_rotate_cannot_resurrect_an_expired_session(
    session_service, user_store, clock, sessions_mod
):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    clock.advance(timedelta(hours=2))

    with pytest.raises(sessions_mod.InvalidSession):
        await session_service.rotate(issued.session_id)


# ---------------------------------------------------------------------------
# revoke / revoke_all_for_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_is_idempotent(session_service, user_store):
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)

    await session_service.revoke(issued.session_id)
    await session_service.revoke(issued.session_id)  # does not raise


@pytest.mark.asyncio
async def test_revoke_never_raises_on_a_missing_or_unknown_id(session_service, sessions_mod):
    # Logout must be safe to retry, and must not become an oracle that
    # distinguishes real session ids from fabricated ones.
    await session_service.revoke(None)
    await session_service.revoke("")
    await session_service.revoke(sessions_mod.generate_session_id())
    await session_service.revoke("not-even-a-plausible-id")


@pytest.mark.asyncio
async def test_revoke_retains_the_row_rather_than_deleting_it(
    session_service, session_store, user_store
):
    # Same "retain, don't delete" posture RefreshRecord takes -- a replay
    # of a logged-out session is auditable as revoked, not merely absent.
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    await session_service.revoke(issued.session_id)

    records = session_store.all_records()
    assert len(records) == 1
    assert records[0].revoked is True


@pytest.mark.asyncio
async def test_revoke_all_for_user_kills_every_device(
    session_service, user_store, sessions_mod
):
    user = await user_store.create("alice@example.com", "hash", ())
    other = await user_store.create("bob@example.com", "hash", ())
    alice_sessions = [await session_service.create(user) for _ in range(3)]
    bob_session = await session_service.create(other)

    await session_service.revoke_all_for_user(user.id)

    for issued in alice_sessions:
        with pytest.raises(sessions_mod.InvalidSession):
            await session_service.resolve(issued.session_id)
    # Another user's sessions are untouched.
    assert (await session_service.resolve(bob_session.session_id)).sub == other.id


@pytest.mark.asyncio
async def test_revoke_all_for_user_is_a_no_op_for_a_user_with_no_sessions(session_service):
    await session_service.revoke_all_for_user("a-user-who-never-logged-in")


# ---------------------------------------------------------------------------
# Audit events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_reasons_are_recorded_server_side_only(
    sessions_mod, session_store, user_store, clock, event_sink
):
    # The distinction the wire response withholds still exists -- it simply
    # never leaves the server. This is the audit half of InvalidSession's
    # "don't leak which specific reason" posture.
    service = sessions_mod.SessionService(
        session_store, user_store, clock, idle_ttl=timedelta(hours=1), events=event_sink
    )
    user = await user_store.create("alice@example.com", "hash", ())
    revoked = await service.create(user)
    await service.revoke(revoked.session_id)
    idle = await service.create(user)

    with pytest.raises(sessions_mod.InvalidSession):
        await service.resolve(sessions_mod.generate_session_id())
    with pytest.raises(sessions_mod.InvalidSession):
        await service.resolve(revoked.session_id)
    clock.advance(timedelta(hours=2))
    with pytest.raises(sessions_mod.InvalidSession):
        await service.resolve(idle.session_id)

    rejections = [event for event in event_sink.events if event[0] == "auth.session.rejected"]
    assert [event[1]["reason"] for event in rejections] == [
        "unknown_session",
        "revoked",
        "idle_expiry",
    ]
    assert all(event[1]["outcome"] == "failure" for event in rejections)


@pytest.mark.asyncio
async def test_rotate_and_revoke_emit_their_own_events(
    sessions_mod, session_store, user_store, clock, event_sink
):
    service = sessions_mod.SessionService(session_store, user_store, clock, events=event_sink)
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await service.create(user)
    rotated = await service.rotate(issued.session_id)
    await service.revoke(rotated.session_id)
    await service.revoke_all_for_user(user.id)

    assert [event[0] for event in event_sink.events] == [
        "auth.session.created",
        "auth.session.rotated",
        "auth.session.revoked",
        "auth.session.revoked_all",
    ]


@pytest.mark.asyncio
async def test_no_events_are_emitted_without_a_sink(session_service, user_store):
    # The default (events=None) path must stay entirely silent -- asserted
    # by the absence of any attribute error or extra store interaction.
    user = await user_store.create("alice@example.com", "hash", ())
    issued = await session_service.create(user)
    await session_service.resolve(issued.session_id)
    await session_service.revoke(issued.session_id)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"idle_ttl": timedelta(0)},
        {"idle_ttl": timedelta(seconds=-1)},
        {"absolute_ttl": timedelta(0)},
        {"absolute_ttl": timedelta(seconds=-1)},
        {"touch_interval": timedelta(seconds=-1)},
    ],
)
def test_nonsensical_ttls_are_rejected_at_construction(
    sessions_mod, session_store, user_store, clock, kwargs
):
    # Fail loudly at wiring time rather than silently expiring (or never
    # expiring) every session at runtime.
    with pytest.raises(ValueError):
        sessions_mod.SessionService(session_store, user_store, clock, **kwargs)


def test_ttls_are_readable_back_off_the_service(sessions_mod, session_store, user_store, clock):
    service = sessions_mod.SessionService(
        session_store, user_store, clock, idle_ttl=timedelta(hours=3), absolute_ttl=timedelta(days=2)
    )
    assert service.idle_ttl == timedelta(hours=3)
    assert service.absolute_ttl == timedelta(days=2)


# ---------------------------------------------------------------------------
# Adapter cross-checks
# ---------------------------------------------------------------------------


def test_both_adapters_map_invalid_session_identically(sessions_mod, fastapi_mod, django_mod):
    expected = (401, "unauthenticated")
    assert fastapi_mod.AUTH_ERROR_HTTP[sessions_mod.InvalidSession] == expected
    assert django_mod.AUTH_ERROR_HTTP[sessions_mod.InvalidSession] == expected


def test_sessions_module_imports_no_framework(sessions_mod):
    # The same standalone-importability guarantee _core.py/_cookies.py
    # carry, asserted statically so a future edit that reaches for
    # fastapi/django/sqlalchemy/jwt fails here.
    import inspect

    source = inspect.getsource(sessions_mod)
    for forbidden in ("import fastapi", "import django", "import sqlalchemy", "import jwt"):
        assert forbidden not in source
