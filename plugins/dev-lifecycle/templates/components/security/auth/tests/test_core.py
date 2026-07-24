"""Exhaustive tests for auth's _core.py: Argon2id password hashing, PyJWT
access/refresh tokens, and -- the security-critical core of this
component -- the refresh-token rotation-with-reuse-detection state
machine. Async tests use explicit `@pytest.mark.asyncio` markers --
pytest-asyncio's default "strict" mode picks them up with no extra
`--asyncio-mode` flag or ini configuration needed (matches this catalog's
db-session component)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import TEST_SIGNING_KEY

# ---------------------------------------------------------------------------
# PasswordService
# ---------------------------------------------------------------------------


def test_hash_does_not_return_plaintext(password_service):
    hashed = password_service.hash("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"
    assert hashed.startswith("$argon2id$")


def test_verify_true_for_correct_password(password_service):
    hashed = password_service.hash("correct-horse-battery-staple")
    assert password_service.verify(hashed, "correct-horse-battery-staple") is True


def test_verify_false_for_wrong_password(password_service):
    hashed = password_service.hash("correct-horse-battery-staple")
    assert password_service.verify(hashed, "wrong-password") is False


def test_verify_false_for_malformed_stored_hash(password_service):
    # Not a valid Argon2 hash string at all -- InvalidHashError internally,
    # collapsed to False like a mismatch (see PasswordService.verify's
    # docstring on not distinguishing the two).
    assert password_service.verify("not-an-argon2-hash", "anything") is False


def test_needs_rehash_false_on_a_fresh_hash(password_service):
    hashed = password_service.hash("correct-horse-battery-staple")
    assert password_service.needs_rehash(hashed) is False


def test_needs_rehash_true_when_parameters_changed(core_mod):
    from argon2 import PasswordHasher

    weak_service = core_mod.PasswordService(PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    weak_hash = weak_service.hash("correct-horse-battery-staple")

    strong_service = core_mod.PasswordService(PasswordHasher(time_cost=4, memory_cost=1024, parallelism=2))
    assert strong_service.needs_rehash(weak_hash) is True


def test_dummy_verify_runs_without_raising(password_service):
    password_service.dummy_verify()  # must not raise


def test_dummy_verify_is_independent_of_input(password_service):
    # dummy_verify() takes no arguments -- calling it repeatedly must
    # never raise or otherwise vary in a way that leaks anything.
    for _ in range(3):
        password_service.dummy_verify()


# ---------------------------------------------------------------------------
# TokenService: minting and round-tripping
# ---------------------------------------------------------------------------


def test_access_token_round_trips(token_service):
    token = token_service.mint_access("user-1", ["admin", "user"])
    claims = token_service.decode_access(token)
    assert claims.sub == "user-1"
    assert claims.roles == ["admin", "user"]
    assert claims.jti


def test_refresh_token_round_trips(token_service):
    token, minted_claims = token_service.mint_refresh("user-1", "family-1")
    decoded_claims = token_service.decode_refresh(token)
    assert decoded_claims.sub == "user-1"
    assert decoded_claims.family_id == "family-1"
    assert decoded_claims.jti == minted_claims.jti
    assert decoded_claims.expires_at == minted_claims.expires_at


def test_mint_refresh_returns_matching_raw_token_and_claims(token_service):
    token, claims = token_service.mint_refresh("user-1", "family-1")
    # The claims returned directly must match what decoding the raw token
    # separately produces -- the whole point of returning both together.
    redecoded = token_service.decode_refresh(token)
    assert claims.sub == redecoded.sub
    assert claims.jti == redecoded.jti
    assert claims.family_id == redecoded.family_id


def test_each_minted_token_has_a_unique_jti(token_service):
    token_a = token_service.mint_access("user-1", [])
    token_b = token_service.mint_access("user-1", [])
    claims_a = token_service.decode_access(token_a)
    claims_b = token_service.decode_access(token_b)
    assert claims_a.jti != claims_b.jti


def test_tampered_signature_is_rejected(core_mod, token_service):
    # Tampers with the FIRST character of the signature segment, not the
    # last. The last base64url character of a 32-byte HMAC-SHA256 signature
    # (43 unpadded chars) encodes only the final 2 significant bits, so
    # several distinct characters there decode to the SAME signature bytes
    # -- flipping it therefore left the token validly signed roughly one
    # run in six, which made this test flaky. Every character earlier in
    # the segment carries a full 6 bits, so changing one always changes the
    # decoded signature.
    header, payload, signature = token_service.mint_access("user-1", []).split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    assert flipped != signature
    with pytest.raises(core_mod.InvalidToken):
        token_service.decode_access(f"{header}.{payload}.{flipped}")


def test_expired_access_token_is_rejected(core_mod, token_service, clock):
    token = token_service.mint_access("user-1", [])
    clock.advance(timedelta(minutes=6))  # past the 5-minute access_ttl
    with pytest.raises(core_mod.InvalidToken):
        token_service.decode_access(token)


def test_expired_refresh_token_is_rejected(core_mod, token_service, clock):
    token, _claims = token_service.mint_refresh("user-1", "family-1")
    clock.advance(timedelta(days=8))  # past the 7-day refresh_ttl
    with pytest.raises(core_mod.InvalidToken):
        token_service.decode_refresh(token)


def test_token_valid_up_to_but_not_past_ttl(token_service, clock):
    token = token_service.mint_access("user-1", [])
    clock.advance(timedelta(minutes=4, seconds=59))  # just under the 5-minute ttl
    token_service.decode_access(token)  # must not raise


def test_wrong_secret_is_rejected(core_mod, token_service, clock):
    token = token_service.mint_access("user-1", [])
    other = core_mod.TokenService(
        "a-completely-different-signing-key-value",
        issuer="test-issuer",
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(days=7),
        now=clock,
    )
    with pytest.raises(core_mod.InvalidToken):
        other.decode_access(token)


def test_issuer_mismatch_is_rejected(core_mod, clock):
    minting_service = core_mod.TokenService(
        TEST_SIGNING_KEY,
        issuer="issuer-a",
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(days=7),
        now=clock,
    )
    verifying_service = core_mod.TokenService(
        TEST_SIGNING_KEY,
        issuer="issuer-b",
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(days=7),
        now=clock,
    )
    token = minting_service.mint_access("user-1", [])
    with pytest.raises(core_mod.InvalidToken):
        verifying_service.decode_access(token)


def test_access_token_presented_as_refresh_is_rejected(core_mod, token_service):
    access_token = token_service.mint_access("user-1", [])
    with pytest.raises(core_mod.InvalidToken):
        token_service.decode_refresh(access_token)


def test_refresh_token_presented_as_access_is_rejected(core_mod, token_service):
    refresh_token, _claims = token_service.mint_refresh("user-1", "family-1")
    with pytest.raises(core_mod.InvalidToken):
        token_service.decode_access(refresh_token)


def test_malformed_token_string_is_rejected(core_mod, token_service):
    with pytest.raises(core_mod.InvalidToken):
        token_service.decode_access("this-is-not-a-jwt-at-all")


def test_token_service_rejects_empty_signing_key(core_mod, clock):
    with pytest.raises(ValueError):
        core_mod.TokenService(
            "",
            issuer="test-issuer",
            access_ttl=timedelta(minutes=5),
            refresh_ttl=timedelta(days=7),
            now=clock,
        )


def test_hash_token_is_sha256_hex(core_mod):
    digest = core_mod.hash_token("some-raw-token-value")
    assert len(digest) == 64
    int(digest, 16)  # must be valid hex


def test_hash_token_is_deterministic(core_mod):
    assert core_mod.hash_token("same-value") == core_mod.hash_token("same-value")


def test_hash_token_differs_for_different_input(core_mod):
    assert core_mod.hash_token("value-a") != core_mod.hash_token("value-b")


# ---------------------------------------------------------------------------
# AuthService.register
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_a_user(auth_service):
    user = await auth_service.register("alice@example.com", "hunter2-plus-extra")
    assert user.email == "alice@example.com"
    assert user.password_hash != "hunter2-plus-extra"


@pytest.mark.asyncio
async def test_register_duplicate_email_raises(core_mod, auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    with pytest.raises(core_mod.EmailAlreadyExists):
        await auth_service.register("alice@example.com", "different-password")


@pytest.mark.asyncio
async def test_register_normalizes_email_case_and_whitespace(auth_service):
    user = await auth_service.register("  Alice@Example.COM  ", "hunter2-plus-extra")
    assert user.email == "alice@example.com"


@pytest.mark.asyncio
async def test_register_duplicate_detected_across_normalization(core_mod, auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    with pytest.raises(core_mod.EmailAlreadyExists):
        await auth_service.register("  ALICE@EXAMPLE.COM", "different-password")


@pytest.mark.asyncio
async def test_register_stores_roles(auth_service):
    user = await auth_service.register("alice@example.com", "hunter2-plus-extra", roles=("admin",))
    assert user.roles == ("admin",)


# ---------------------------------------------------------------------------
# AuthService.login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success_returns_usable_pair(auth_service, token_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    claims = token_service.decode_access(pair.access)
    assert claims.sub  # a real, decodable access token
    refresh_claims = token_service.decode_refresh(pair.refresh)
    assert refresh_claims.sub == claims.sub


@pytest.mark.asyncio
async def test_login_unknown_email_raises_invalid_credentials(core_mod, auth_service):
    with pytest.raises(core_mod.InvalidCredentials):
        await auth_service.login("nobody@example.com", "whatever-password")


@pytest.mark.asyncio
async def test_login_unknown_email_does_not_raise_or_leak_via_dummy_verify(auth_service, monkeypatch, core_mod):
    calls = []
    original = core_mod.PasswordService.dummy_verify

    def spy(self):
        calls.append(True)
        return original(self)

    monkeypatch.setattr(core_mod.PasswordService, "dummy_verify", spy)
    with pytest.raises(core_mod.InvalidCredentials):
        await auth_service.login("nobody@example.com", "whatever-password")
    assert calls == [True]  # the timing-defense path was actually taken


@pytest.mark.asyncio
async def test_login_wrong_password_raises_invalid_credentials(core_mod, auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    with pytest.raises(core_mod.InvalidCredentials):
        await auth_service.login("alice@example.com", "wrong-password")


@pytest.mark.asyncio
async def test_login_wrong_password_and_unknown_email_raise_the_same_exception_type(core_mod, auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    with pytest.raises(core_mod.InvalidCredentials) as wrong_pw_exc:
        await auth_service.login("alice@example.com", "wrong-password")
    with pytest.raises(core_mod.InvalidCredentials) as unknown_exc:
        await auth_service.login("nobody@example.com", "wrong-password")
    # Same exception type AND same message -- no distinguishing signal.
    assert type(wrong_pw_exc.value) is type(unknown_exc.value)
    assert str(wrong_pw_exc.value) == str(unknown_exc.value)


@pytest.mark.asyncio
async def test_login_persists_a_refresh_record(auth_service, refresh_store, core_mod):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    record = await refresh_store.get_by_hash(core_mod.hash_token(pair.refresh))
    assert record is not None
    assert record.used_at is None
    assert record.revoked is False


# ---------------------------------------------------------------------------
# AuthService.refresh -- THE rotation-with-reuse-detection state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_happy_path_rotates(auth_service, refresh_store, token_service, core_mod):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")

    new_pair = await auth_service.refresh(pair.refresh)

    # New pair differs from the old one entirely.
    assert new_pair.access != pair.access
    assert new_pair.refresh != pair.refresh

    # Old row now has used_at set.
    old_row = await refresh_store.get_by_hash(core_mod.hash_token(pair.refresh))
    assert old_row is not None
    assert old_row.used_at is not None
    assert old_row.revoked is False

    # New row present and NOT used.
    new_row = await refresh_store.get_by_hash(core_mod.hash_token(new_pair.refresh))
    assert new_row is not None
    assert new_row.used_at is None

    # Same family.
    new_claims = token_service.decode_refresh(new_pair.refresh)
    old_claims = token_service.decode_refresh(pair.refresh)
    assert new_claims.family_id == old_claims.family_id
    assert old_row.family_id == new_row.family_id


@pytest.mark.asyncio
async def test_refresh_reuse_is_detected_and_revokes_the_whole_family(auth_service, refresh_store, core_mod):
    """THE crown-jewel test: rotate once, then present the ORIGINAL
    (now-used) refresh token again -- must raise TokenReused AND kill
    every row in that family, including the freshly-minted valid child."""
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    original_pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")

    rotated_pair = await auth_service.refresh(original_pair.refresh)

    # Present the original (already-used) token again.
    with pytest.raises(core_mod.TokenReused):
        await auth_service.refresh(original_pair.refresh)

    # The whole family is now revoked -- verify directly on the store.
    original_row = await refresh_store.get_by_hash(core_mod.hash_token(original_pair.refresh))
    rotated_row = await refresh_store.get_by_hash(core_mod.hash_token(rotated_pair.refresh))
    assert original_row is not None and original_row.revoked is True
    assert rotated_row is not None and rotated_row.revoked is True

    # The just-minted, otherwise-still-valid child ALSO stops working now.
    with pytest.raises(core_mod.InvalidToken):
        await auth_service.refresh(rotated_pair.refresh)


@pytest.mark.asyncio
async def test_refresh_from_an_already_revoked_family_is_invalid_token(auth_service, refresh_store, core_mod):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    await auth_service.logout(pair.refresh)  # revokes the family

    with pytest.raises(core_mod.InvalidToken):
        await auth_service.refresh(pair.refresh)


@pytest.mark.asyncio
async def test_refresh_unknown_but_validly_signed_token_is_invalid_token(
    auth_service, refresh_store, token_service, core_mod
):
    # A validly-signed refresh JWT that was never persisted (e.g. minted
    # by a different, unrelated flow, or the store lost the row).
    forged_but_validly_signed, _claims = token_service.mint_refresh("some-user-id", "some-family-id")

    with pytest.raises(core_mod.InvalidToken):
        await auth_service.refresh(forged_but_validly_signed)

    # Critically: no row existed, so nothing should have been revoked --
    # the state machine must not trust the token's own claims to revoke
    # a family that was never on file.
    assert refresh_store.revoke_family_calls == []


@pytest.mark.asyncio
async def test_refresh_expired_row_is_invalid_token(auth_service, clock, core_mod):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")

    clock.advance(timedelta(days=8))  # past the 7-day refresh_ttl

    with pytest.raises(core_mod.InvalidToken):
        await auth_service.refresh(pair.refresh)


@pytest.mark.asyncio
async def test_refresh_chain_can_rotate_multiple_times(auth_service, token_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")

    original_family = token_service.decode_refresh(pair.refresh).family_id

    for _ in range(5):
        pair = await auth_service.refresh(pair.refresh)
        assert token_service.decode_refresh(pair.refresh).family_id == original_family

    # The latest token in the chain is still valid.
    final_pair = await auth_service.refresh(pair.refresh)
    assert final_pair is not None


@pytest.mark.asyncio
async def test_refresh_with_garbage_token_raises_invalid_token(core_mod, auth_service):
    with pytest.raises(core_mod.InvalidToken):
        await auth_service.refresh("not-a-jwt-at-all")


@pytest.mark.asyncio
async def test_refresh_with_an_access_token_raises_invalid_token(core_mod, auth_service, token_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    with pytest.raises(core_mod.InvalidToken):
        await auth_service.refresh(pair.access)


# ---------------------------------------------------------------------------
# AuthService.logout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logout_revokes_the_family(auth_service, refresh_store, core_mod):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")

    await auth_service.logout(pair.refresh)

    row = await refresh_store.get_by_hash(core_mod.hash_token(pair.refresh))
    assert row is not None
    assert row.revoked is True


@pytest.mark.asyncio
async def test_logout_then_refresh_with_any_family_token_is_invalid(auth_service, core_mod):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    rotated_pair = await auth_service.refresh(pair.refresh)

    await auth_service.logout(rotated_pair.refresh)

    with pytest.raises(core_mod.InvalidToken):
        await auth_service.refresh(rotated_pair.refresh)


@pytest.mark.asyncio
async def test_logout_with_garbage_token_does_not_raise(auth_service):
    await auth_service.logout("totally-not-a-jwt")  # must not raise


@pytest.mark.asyncio
async def test_logout_with_unknown_but_validly_signed_token_does_not_raise(auth_service, token_service):
    forged_but_validly_signed, _claims = token_service.mint_refresh("some-user-id", "some-family-id")
    await auth_service.logout(forged_but_validly_signed)  # must not raise


@pytest.mark.asyncio
async def test_logout_is_idempotent(auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    await auth_service.logout(pair.refresh)
    await auth_service.logout(pair.refresh)  # second call must not raise either


@pytest.mark.asyncio
async def test_logout_with_an_access_token_does_not_raise(auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    await auth_service.logout(pair.access)  # wrong type -- decode fails -- swallowed, no raise


# ---------------------------------------------------------------------------
# AuthService.resolve_access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_access_returns_claims_with_roles(auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra", roles=("admin", "user"))
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")

    claims = await auth_service.resolve_access(pair.access)

    assert claims.roles == ["admin", "user"]
    assert claims.sub


@pytest.mark.asyncio
async def test_resolve_access_invalid_token_raises(core_mod, auth_service):
    with pytest.raises(core_mod.InvalidToken):
        await auth_service.resolve_access("garbage-token")


@pytest.mark.asyncio
async def test_resolve_access_rejects_a_refresh_token(core_mod, auth_service):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    with pytest.raises(core_mod.InvalidToken):
        await auth_service.resolve_access(pair.refresh)


@pytest.mark.asyncio
async def test_resolve_access_rejects_an_expired_access_token(core_mod, auth_service, clock):
    await auth_service.register("alice@example.com", "hunter2-plus-extra")
    pair = await auth_service.login("alice@example.com", "hunter2-plus-extra")
    clock.advance(timedelta(minutes=6))
    with pytest.raises(core_mod.InvalidToken):
        await auth_service.resolve_access(pair.access)


# ---------------------------------------------------------------------------
# SingleUseTokenService: issue/consume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_use_token_issue_consume_happy_path(core_mod, single_use_token_service, single_use_token_store):
    raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=24))
    user_id = await single_use_token_service.consume(raw, "verify")
    assert user_id == "user-1"

    # The row is marked used.
    stored = await single_use_token_store.get_by_hash(core_mod.hash_token(raw))
    assert stored is not None
    assert stored.used_at is not None


@pytest.mark.asyncio
async def test_single_use_token_raw_is_not_the_stored_hash(single_use_token_service, single_use_token_store, core_mod):
    raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=24))
    stored = await single_use_token_store.get_by_hash(core_mod.hash_token(raw))
    assert stored is not None
    assert stored.token_hash != raw


@pytest.mark.asyncio
async def test_single_use_token_reuse_raises_invalid_single_use_token(core_mod, single_use_token_service):
    raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=24))
    await single_use_token_service.consume(raw, "verify")
    with pytest.raises(core_mod.InvalidSingleUseToken):
        await single_use_token_service.consume(raw, "verify")


@pytest.mark.asyncio
async def test_single_use_token_expired_raises_invalid_single_use_token(core_mod, single_use_token_service, clock):
    raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=24))
    clock.advance(timedelta(hours=25))
    with pytest.raises(core_mod.InvalidSingleUseToken):
        await single_use_token_service.consume(raw, "verify")


@pytest.mark.asyncio
async def test_single_use_token_wrong_purpose_raises_invalid_single_use_token(core_mod, single_use_token_service):
    raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=24))
    with pytest.raises(core_mod.InvalidSingleUseToken):
        await single_use_token_service.consume(raw, "reset")


@pytest.mark.asyncio
async def test_single_use_token_unknown_raises_invalid_single_use_token(core_mod, single_use_token_service):
    with pytest.raises(core_mod.InvalidSingleUseToken):
        await single_use_token_service.consume("not-a-real-token", "verify")


@pytest.mark.asyncio
async def test_single_use_token_failure_modes_are_the_same_exception_type(core_mod, single_use_token_service, clock):
    """reuse, expired, wrong-purpose, and unknown all raise the SAME
    exception type -- InvalidSingleUseToken is deliberately generic (see
    its own docstring)."""
    reused_raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=1))
    await single_use_token_service.consume(reused_raw, "verify")
    with pytest.raises(core_mod.InvalidSingleUseToken) as reuse_exc:
        await single_use_token_service.consume(reused_raw, "verify")

    expired_raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=1))
    clock.advance(timedelta(hours=2))
    with pytest.raises(core_mod.InvalidSingleUseToken) as expired_exc:
        await single_use_token_service.consume(expired_raw, "verify")
    clock.advance(-timedelta(hours=2))  # rewind for the rest of this test

    wrong_purpose_raw = await single_use_token_service.issue("user-1", "verify", timedelta(hours=1))
    with pytest.raises(core_mod.InvalidSingleUseToken) as purpose_exc:
        await single_use_token_service.consume(wrong_purpose_raw, "reset")

    with pytest.raises(core_mod.InvalidSingleUseToken) as unknown_exc:
        await single_use_token_service.consume("totally-unknown", "verify")

    assert type(reuse_exc.value) is type(expired_exc.value) is type(purpose_exc.value) is type(unknown_exc.value)


# ---------------------------------------------------------------------------
# LockoutPolicy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lockout_below_threshold_is_not_locked(core_mod, lockout_store, clock):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=5, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    for _ in range(4):
        just_locked = await policy.record_failure("account-a")
        assert just_locked is False
    assert await policy.is_locked("account-a") is False


@pytest.mark.asyncio
async def test_lockout_nth_failure_crosses_threshold_exactly_once(core_mod, lockout_store, clock):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=3, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    assert await policy.record_failure("account-a") is False
    assert await policy.record_failure("account-a") is False
    assert await policy.record_failure("account-a") is True  # crosses here
    assert await policy.record_failure("account-a") is False  # already locked, not a NEW crossing
    assert await policy.is_locked("account-a") is True


@pytest.mark.asyncio
async def test_lockout_is_locked_false_after_locked_until_passes(core_mod, lockout_store, clock):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=2, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    await policy.record_failure("account-a")
    await policy.record_failure("account-a")
    assert await policy.is_locked("account-a") is True
    clock.advance(timedelta(minutes=16))
    assert await policy.is_locked("account-a") is False


@pytest.mark.asyncio
async def test_lockout_rolling_window_resets_the_count(core_mod, lockout_store, clock):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=3, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    await policy.record_failure("account-a")
    await policy.record_failure("account-a")
    # Past the rolling window since the last failure -- the streak resets.
    clock.advance(timedelta(minutes=11))
    just_locked = await policy.record_failure("account-a")
    assert just_locked is False  # fresh count of 1, not 3 -- did not cross
    assert await policy.is_locked("account-a") is False


@pytest.mark.asyncio
async def test_lockout_clear_unlocks_the_account(core_mod, lockout_store, clock):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=2, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    await policy.record_failure("account-a")
    await policy.record_failure("account-a")
    assert await policy.is_locked("account-a") is True
    await policy.clear("account-a")
    assert await policy.is_locked("account-a") is False


@pytest.mark.asyncio
async def test_lockout_is_per_account(core_mod, lockout_store, clock):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=2, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    await policy.record_failure("account-a")
    await policy.record_failure("account-a")
    assert await policy.is_locked("account-a") is True
    assert await policy.is_locked("account-b") is False


# ---------------------------------------------------------------------------
# AuthService.login integration -- lockout, require_verification, events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_with_lockout_locks_after_repeated_wrong_passwords(
    core_mod, user_store, refresh_store, password_service, token_service, lockout_store, clock
):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=3, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    service = core_mod.AuthService(user_store, refresh_store, password_service, token_service, clock, lockout=policy)
    await service.register("alice@example.com", "correct-password-1")

    for _ in range(3):
        with pytest.raises(core_mod.InvalidCredentials):
            await service.login("alice@example.com", "wrong-password")

    user = await user_store.get_by_email("alice@example.com")
    assert await policy.is_locked(user.id) is True

    # Even the CORRECT password is rejected -- generically -- while locked.
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "correct-password-1")

    # After the lock expires, the correct password succeeds again.
    clock.advance(timedelta(minutes=16))
    pair = await service.login("alice@example.com", "correct-password-1")
    assert pair.access


@pytest.mark.asyncio
async def test_login_success_clears_the_lockout_counter(
    core_mod, user_store, refresh_store, password_service, token_service, lockout_store, clock
):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=3, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    service = core_mod.AuthService(user_store, refresh_store, password_service, token_service, clock, lockout=policy)
    await service.register("alice@example.com", "correct-password-1")

    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "wrong-password")
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "wrong-password")

    await service.login("alice@example.com", "correct-password-1")  # success clears the counter

    user = await user_store.get_by_email("alice@example.com")
    record = await lockout_store.get(user.id)
    assert record is None

    # Two more wrong guesses after a successful login should NOT already
    # be at the threshold (the counter was reset by the success above).
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "wrong-password")
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "wrong-password")
    assert await policy.is_locked(user.id) is False


@pytest.mark.asyncio
async def test_login_require_verification_blocks_unverified_user(
    core_mod, user_store, refresh_store, password_service, token_service, clock
):
    service = core_mod.AuthService(
        user_store, refresh_store, password_service, token_service, clock, require_verification=True
    )
    await service.register("alice@example.com", "correct-password-1")

    # Correct password, but the email was never verified -- generic failure.
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "correct-password-1")

    user = await user_store.get_by_email("alice@example.com")
    await user_store.mark_email_verified(user.id, clock())

    pair = await service.login("alice@example.com", "correct-password-1")
    assert pair.access


@pytest.mark.asyncio
async def test_every_failing_login_path_raises_the_same_generic_exception_type(
    core_mod, user_store, refresh_store, password_service, token_service, lockout_store, clock
):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=2, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    service = core_mod.AuthService(
        user_store,
        refresh_store,
        password_service,
        token_service,
        clock,
        lockout=policy,
        require_verification=True,
    )
    await service.register("alice@example.com", "correct-password-1")
    user = await user_store.get_by_email("alice@example.com")

    exceptions = []

    # unknown email
    try:
        await service.login("nobody@example.com", "whatever")
    except core_mod.AuthError as exc:
        exceptions.append(exc)

    # wrong password
    try:
        await service.login("alice@example.com", "wrong-password")
    except core_mod.AuthError as exc:
        exceptions.append(exc)

    # unverified email, correct password
    try:
        await service.login("alice@example.com", "correct-password-1")
    except core_mod.AuthError as exc:
        exceptions.append(exc)

    # cross the lockout threshold
    try:
        await service.login("alice@example.com", "wrong-password")
    except core_mod.AuthError as exc:
        exceptions.append(exc)
    assert await policy.is_locked(user.id) is True

    # locked account, correct password
    try:
        await service.login("alice@example.com", "correct-password-1")
    except core_mod.AuthError as exc:
        exceptions.append(exc)

    assert len(exceptions) == 5
    assert all(type(exc) is core_mod.InvalidCredentials for exc in exceptions)
    assert len({str(exc) for exc in exceptions}) == 1  # every message identical too


@pytest.mark.asyncio
async def test_login_events_are_emitted_for_success_failure_denied_and_lockout(
    core_mod, user_store, refresh_store, password_service, token_service, lockout_store, event_sink, clock
):
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=2, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    service = core_mod.AuthService(
        user_store, refresh_store, password_service, token_service, clock, lockout=policy, events=event_sink
    )
    await service.register("alice@example.com", "correct-password-1")
    user = await user_store.get_by_email("alice@example.com")

    # Success.
    await service.login("alice@example.com", "correct-password-1")
    assert ("auth.login", {"actor": user.id, "outcome": "success"}) in event_sink.events

    # Wrong password -> failure (first), then crosses the threshold -> lockout.triggered + failure.
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "wrong-password")
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "wrong-password")

    failure_events = [e for e in event_sink.events if e == ("auth.login", {"actor": user.id, "outcome": "failure"})]
    assert len(failure_events) == 2
    assert ("auth.lockout.triggered", {"actor": user.id, "outcome": "denied"}) in event_sink.events

    # Locked -> denied.
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("alice@example.com", "correct-password-1")
    assert ("auth.login", {"actor": user.id, "outcome": "denied"}) in event_sink.events

    # Unknown email -> failure, actor "anonymous".
    with pytest.raises(core_mod.InvalidCredentials):
        await service.login("nobody@example.com", "whatever")
    assert ("auth.login", {"actor": "anonymous", "outcome": "failure"}) in event_sink.events


# ---------------------------------------------------------------------------
# AccountService: email verification + password reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_service_request_email_verification_sends_the_token(
    core_mod, auth_service, account_service, email_sender, single_use_token_store
):
    user = await auth_service.register("alice@example.com", "hunter2-plus-extra")

    await account_service.request_email_verification(user)

    assert len(email_sender.sent) == 1
    message = email_sender.sent[0]
    assert message.to == "alice@example.com"
    assert "verify-email#token=" in message.body

    # A "verify" token was actually persisted, hashed.
    stored = single_use_token_store._by_hash
    assert len(stored) == 1
    record = next(iter(stored.values()))
    assert record.purpose == "verify"
    assert record.user_id == user.id
    assert record.token_hash != message.body  # not the raw body


@pytest.mark.asyncio
async def test_account_service_verify_email_flips_email_verified(core_mod, auth_service, account_service, email_sender, user_store):
    user = await auth_service.register("alice@example.com", "hunter2-plus-extra")
    assert user.email_verified is False

    await account_service.request_email_verification(user)
    raw_link_body = email_sender.sent[0].body
    raw_token = raw_link_body.split("token=")[1].splitlines()[0]

    result = await account_service.verify_email(raw_token)
    assert result is None  # API-facing method returns nothing, never the token

    updated = await user_store.get_by_id(user.id)
    assert updated.email_verified is True


@pytest.mark.asyncio
async def test_account_service_verify_email_bad_token_raises(core_mod, account_service):
    with pytest.raises(core_mod.InvalidSingleUseToken):
        await account_service.verify_email("not-a-real-token")


@pytest.mark.asyncio
async def test_account_service_request_password_reset_known_email_sends_token(
    core_mod, auth_service, account_service, email_sender, single_use_token_store
):
    await auth_service.register("alice@example.com", "old-password-1")

    result = await account_service.request_password_reset("alice@example.com")
    assert result is None  # never reveals anything to the caller

    assert len(email_sender.sent) == 1
    message = email_sender.sent[0]
    assert message.to == "alice@example.com"
    assert "reset-password#token=" in message.body
    assert "old-password-1" not in message.body  # never the password

    stored = single_use_token_store._by_hash
    assert len(stored) == 1
    record = next(iter(stored.values()))
    assert record.purpose == "reset"


@pytest.mark.asyncio
async def test_account_service_request_password_reset_unknown_email_sends_nothing(
    core_mod, account_service, email_sender, single_use_token_store, event_sink
):
    # Must not raise, must not send an email, must not persist a token --
    # but the not-found path still runs its comparable-cost throwaway hash
    # without erroring.
    result = await account_service.request_password_reset("nobody@example.com")
    assert result is None

    assert email_sender.sent == []
    assert single_use_token_store._by_hash == {}
    assert ("auth.password.reset_requested", {"actor": "user:unknown", "outcome": "success"}) in event_sink.events
    # Never the submitted email as the actor.
    for _action, payload in event_sink.events:
        assert payload["actor"] != "nobody@example.com"


@pytest.mark.asyncio
async def test_account_service_reset_password_changes_hash_and_revokes_sessions(
    core_mod, auth_service, account_service, email_sender, user_store, refresh_store
):
    await auth_service.register("alice@example.com", "old-password-1")
    pair = await auth_service.login("alice@example.com", "old-password-1")  # an active session

    await account_service.request_password_reset("alice@example.com")
    raw_token = email_sender.sent[0].body.split("token=")[1].splitlines()[0]

    result = await account_service.reset_password(raw_token, "new-password-2")
    assert result is None

    # Old password no longer verifies; new one does.
    with pytest.raises(core_mod.InvalidCredentials):
        await auth_service.login("alice@example.com", "old-password-1")
    new_pair = await auth_service.login("alice@example.com", "new-password-2")
    assert new_pair.access

    # The pre-reset session's refresh token is revoked (revoke_all_for_user).
    old_row = await refresh_store.get_by_hash(core_mod.hash_token(pair.refresh))
    assert old_row is not None
    assert old_row.revoked is True


@pytest.mark.asyncio
async def test_reset_password_clears_an_active_lockout(
    core_mod,
    user_store,
    refresh_store,
    password_service,
    token_service,
    single_use_token_service,
    email_sender,
    lockout_store,
    clock,
):
    """A user who tripped the lockout guessing their password, then reset it,
    must be able to log in with the NEW password immediately -- a reset
    restores access rather than leaving them blocked at login's is-locked
    check for the remaining cooldown. AuthService and AccountService share
    the one LockoutPolicy/store, so reset_password's clear lifts the lock."""
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=3, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    auth = core_mod.AuthService(user_store, refresh_store, password_service, token_service, clock, lockout=policy)
    account = core_mod.AccountService(
        user_store,
        single_use_token_service,
        email_sender,
        password_service,
        refresh_store,
        clock,
        lockout=policy,
        frontend_base_url="https://app.example.com",
    )
    await auth.register("alice@example.com", "old-password-1")

    for _ in range(3):
        with pytest.raises(core_mod.InvalidCredentials):
            await auth.login("alice@example.com", "wrong-password")
    user = await user_store.get_by_email("alice@example.com")
    assert await policy.is_locked(user.id) is True

    # Reset WITHOUT advancing the clock -- the lock is still active.
    await account.request_password_reset("alice@example.com")
    raw_token = email_sender.sent[-1].body.split("token=")[1].splitlines()[0]
    await account.reset_password(raw_token, "new-password-2")

    # The lock is lifted and the new password works right away.
    assert await policy.is_locked(user.id) is False
    pair = await auth.login("alice@example.com", "new-password-2")
    assert pair.access


@pytest.mark.asyncio
async def test_account_service_reset_password_bad_token_raises_and_does_not_touch_the_account(
    core_mod, auth_service, account_service, user_store
):
    await auth_service.register("alice@example.com", "old-password-1")
    with pytest.raises(core_mod.InvalidSingleUseToken):
        await account_service.reset_password("not-a-real-token", "new-password-2")

    # The password is untouched.
    pair = await auth_service.login("alice@example.com", "old-password-1")
    assert pair.access


@pytest.mark.asyncio
async def test_account_service_emails_never_contain_a_raw_password(
    core_mod, auth_service, account_service, email_sender
):
    await auth_service.register("alice@example.com", "super-secret-password-1")
    user = await auth_service.register("bob@example.com", "another-secret-password-2")
    await account_service.request_email_verification(user)
    await account_service.request_password_reset("alice@example.com")

    for message in email_sender.sent:
        assert "super-secret-password-1" not in message.body
        assert "another-secret-password-2" not in message.body


# ---------------------------------------------------------------------------
# Stage 5c adversarial-review fix (M1/M2): email delivery failure must never
# change request_password_reset's outcome, and reset_password must also
# verify the email (the recovery path for a registration whose verification
# email failed to send).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_password_reset_known_email_does_not_raise_when_send_fails(
    core_mod, auth_service, user_store, single_use_token_service, password_service, refresh_store, clock,
    raising_email_sender, event_sink,
):
    """M1: a delivery failure on the KNOWN-email branch must never turn
    into an exception -- that would let a caller (an HTTP route with no
    catch, per this component's contract) return a different status code
    for a known vs. an unknown email, which IS the account-enumeration
    oracle this method exists to prevent. The token must still be issued
    and persisted (the failure is in DELIVERY, not in issuance) so the
    account isn't silently left without a usable reset token on file."""
    account = core_mod.AccountService(
        user_store,
        single_use_token_service,
        raising_email_sender,
        password_service,
        refresh_store,
        clock,
        events=event_sink,
        frontend_base_url="https://app.example.com",
    )
    await auth_service.register("alice@example.com", "old-password-1")

    result = await account.request_password_reset("alice@example.com")

    assert result is None  # did NOT raise, returned exactly like the happy path
    assert raising_email_sender.attempts == 1  # send() really was attempted

    user = await user_store.get_by_email("alice@example.com")
    assert user is not None

    # A failure event was emitted instead of the success one.
    assert ("auth.password.reset_email_failed", {"actor": user.id, "outcome": "failure"}) in event_sink.events
    assert not any(action == "auth.password.reset_requested" for action, _ in event_sink.events)


@pytest.mark.asyncio
async def test_request_password_reset_known_email_send_failure_still_issues_a_usable_token(
    core_mod, auth_service, user_store, single_use_token_store, single_use_token_service, password_service,
    refresh_store, clock, raising_email_sender,
):
    """Same failure scenario as above, proven end-to-end: even though the
    email never "arrived" (the sender raised), the reset token it would
    have carried was genuinely issued and persisted -- recoverable e.g. by
    an operator re-sending it, or by the same request being retried
    against a healthy sender."""
    account = core_mod.AccountService(
        user_store,
        single_use_token_service,
        raising_email_sender,
        password_service,
        refresh_store,
        clock,
        frontend_base_url="https://app.example.com",
    )
    await auth_service.register("alice@example.com", "old-password-1")

    await account.request_password_reset("alice@example.com")

    assert len(single_use_token_store._by_hash) == 1
    record = next(iter(single_use_token_store._by_hash.values()))
    assert record.purpose == "reset"
    assert record.used_at is None


@pytest.mark.asyncio
async def test_reset_password_marks_email_verified(
    core_mod, auth_service, account_service, email_sender, user_store,
):
    """M2 recovery path: a user registers (unverified -- `verify_email` is
    never called here, modeling a registration whose verification email
    failed to send, per FIX 2/3), requests a password reset, and completes
    it -- the account is now `email_verified=True` even though `verify_
    email` was never invoked, and can log in under `require_verification=
    True`."""
    user = await auth_service.register("alice@example.com", "old-password-1")
    assert user.email_verified is False

    await account_service.request_password_reset("alice@example.com")
    raw_token = email_sender.sent[-1].body.split("token=")[1].splitlines()[0]
    await account_service.reset_password(raw_token, "new-password-2")

    updated = await user_store.get_by_id(user.id)
    assert updated.email_verified is True

    # Login works even under a require_verification=True AuthService,
    # despite verify_email having never been called.
    verifying_auth = core_mod.AuthService(
        user_store,
        auth_service._refresh_tokens,
        auth_service._passwords,
        auth_service._tokens,
        auth_service._now,
        require_verification=True,
    )
    pair = await verifying_auth.login("alice@example.com", "new-password-2")
    assert pair.access


# ---------------------------------------------------------------------------
# Zero-diff proof: refresh reuse-detection is untouched by this stage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_reuse_detection_still_works_with_a_lockout_wired_auth_service(
    core_mod, user_store, refresh_store, password_service, token_service, lockout_store, clock
):
    """The refresh/reuse state machine is exercised through an AuthService
    that ALSO has a LockoutPolicy wired -- proving the two features are
    fully independent and neither interferes with the other."""
    policy = core_mod.LockoutPolicy(
        lockout_store, max_failures=5, lockout_duration=timedelta(minutes=15), window=timedelta(minutes=10), now=clock
    )
    service = core_mod.AuthService(user_store, refresh_store, password_service, token_service, clock, lockout=policy)
    await service.register("alice@example.com", "hunter2-plus-extra")
    pair = await service.login("alice@example.com", "hunter2-plus-extra")

    rotated_pair = await service.refresh(pair.refresh)

    with pytest.raises(core_mod.TokenReused):
        await service.refresh(pair.refresh)

    original_row = await refresh_store.get_by_hash(core_mod.hash_token(pair.refresh))
    rotated_row = await refresh_store.get_by_hash(core_mod.hash_token(rotated_pair.refresh))
    assert original_row.revoked is True
    assert rotated_row.revoked is True


# ---------------------------------------------------------------------------
# AuthService.authenticate -- the credential half login and the session path share
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_returns_the_user_and_mints_nothing(auth_service, refresh_store):
    # The whole point of this primitive: a session login gets an
    # authenticated UserRecord WITHOUT a RefreshRecord being persisted for
    # a token no client will ever hold.
    registered = await auth_service.register("alice@example.com", "correct-horse-battery-staple")

    user = await auth_service.authenticate("alice@example.com", "correct-horse-battery-staple")

    assert user.id == registered.id
    assert user.email == "alice@example.com"
    assert refresh_store.all_records() == []


@pytest.mark.asyncio
async def test_authenticate_normalizes_email_like_login(auth_service):
    await auth_service.register("alice@example.com", "correct-horse-battery-staple")
    user = await auth_service.authenticate("  Alice@Example.COM  ", "correct-horse-battery-staple")
    assert user.email == "alice@example.com"


@pytest.mark.asyncio
async def test_authenticate_rejects_an_unknown_email(auth_service, core_mod):
    with pytest.raises(core_mod.InvalidCredentials):
        await auth_service.authenticate("nobody@example.com", "any-password")


@pytest.mark.asyncio
async def test_authenticate_rejects_a_wrong_password_identically(auth_service, core_mod):
    await auth_service.register("alice@example.com", "correct-horse-battery-staple")

    with pytest.raises(core_mod.InvalidCredentials) as wrong_password:
        await auth_service.authenticate("alice@example.com", "not-the-password")
    with pytest.raises(core_mod.InvalidCredentials) as unknown_email:
        await auth_service.authenticate("nobody@example.com", "not-the-password")

    # Same user-enumeration defense login has always had -- asserted on the
    # message, since that is what reaches a caller.
    assert str(wrong_password.value) == str(unknown_email.value)


@pytest.mark.asyncio
async def test_login_is_authenticate_plus_a_mint(auth_service, refresh_store, monkeypatch):
    # Guards against the two implementations drifting back apart: login
    # must route its credential checking THROUGH authenticate, not repeat
    # it. Asserted by spying on authenticate and confirming login used it.
    await auth_service.register("alice@example.com", "correct-horse-battery-staple")
    calls = []
    real_authenticate = auth_service.authenticate

    async def spy(email, password):
        calls.append((email, password))
        return await real_authenticate(email, password)

    monkeypatch.setattr(auth_service, "authenticate", spy)
    pair = await auth_service.login("alice@example.com", "correct-horse-battery-staple")

    assert calls == [("alice@example.com", "correct-horse-battery-staple")]
    assert pair.access and pair.refresh
    # ...and the mint half really did happen.
    assert len(refresh_store.all_records()) == 1
