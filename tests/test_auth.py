"""Authentication and the two roles.

The property that matters for the submission is that `viewer` is genuinely
read-only rather than read-only by convention: an evaluator will be handed that
account and pointed at a public URL, and "please do not press delete" is not an
access control.

The rest is the small set of things that are easy to get subtly wrong in a
hand-rolled token — a signature that is not actually checked, an expiry that is
not enforced, a malformed input that raises instead of being rejected — each of
which fails open.
"""

from __future__ import annotations

import time

import pytest

from services.api.auth import (
    Principal,
    hash_password,
    issue_token,
    read_token,
    verify_password,
)


class TestPasswords:
    def test_a_password_verifies_against_its_own_hash(self) -> None:
        stored = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", stored)

    def test_a_wrong_password_does_not(self) -> None:
        assert not verify_password("wrong", hash_password("right"))

    def test_two_hashes_of_one_password_differ(self) -> None:
        """Per-user salt: identical passwords must not be visibly identical."""
        assert hash_password("same") != hash_password("same")

    def test_the_cost_parameters_travel_with_the_hash(self) -> None:
        """So they can be raised later without invalidating existing accounts."""
        scheme, n, r, p, _salt, _hash = hash_password("x").split("$")
        assert scheme == "scrypt"
        assert int(n) >= 2**14 and int(r) >= 8 and int(p) >= 1

    def test_a_malformed_stored_hash_is_a_mismatch_not_a_crash(self) -> None:
        """A crash here would distinguish this account from a well-formed one."""
        for broken in ("", "garbage", "scrypt$notanumber$8$1$aaaa$bbbb", "md5$a$b"):
            assert verify_password("anything", broken) is False


class TestTokens:
    def test_a_token_round_trips(self) -> None:
        payload = read_token(issue_token("demo", "viewer"))
        assert payload is not None
        assert (payload["sub"], payload["role"]) == ("demo", "viewer")

    def test_a_tampered_payload_is_rejected(self) -> None:
        """The whole point of signing. Promoting viewer to operator must fail."""
        import base64
        import json

        token = issue_token("demo", "viewer")
        body, signature = token.split(".")
        forged = base64.urlsafe_b64encode(
            json.dumps({"sub": "demo", "role": "operator", "exp": 2**31}).encode()
        ).rstrip(b"=").decode()
        assert read_token(f"{forged}.{signature}") is None

    def test_an_expired_token_is_rejected(self) -> None:
        assert read_token(issue_token("demo", "viewer", ttl_s=-1)) is None

    def test_a_token_expiring_now_is_not_still_valid(self) -> None:
        token = issue_token("demo", "viewer", ttl_s=0)
        time.sleep(0.01)
        assert read_token(token) is None

    @pytest.mark.parametrize("junk", ["", "no-dot", "a.b.c", "....", "x." + "y" * 200])
    def test_malformed_tokens_are_rejected_without_raising(self, junk: str) -> None:
        assert read_token(junk) is None


class TestRoles:
    def test_a_viewer_cannot_write(self) -> None:
        """What makes the demo account read-only rather than politely asked."""
        assert not Principal(username="demo", role="viewer").can_write

    def test_an_operator_can(self) -> None:
        assert Principal(username="duty", role="operator").can_write

    def test_an_unknown_role_cannot_write(self) -> None:
        """Fail closed: a role this code does not recognise gets no privileges."""
        assert not Principal(username="odd", role="superuser").can_write

    def test_the_default_role_is_the_least_privileged(self) -> None:
        assert not Principal(username="someone").can_write


class TestRequireOperator:
    def test_it_refuses_a_viewer_with_403_not_401(self) -> None:
        """401 means "log in"; this account *is* logged in and still may not."""
        from fastapi import HTTPException

        from services.api.auth import require_operator

        with pytest.raises(HTTPException) as raised:
            require_operator(Principal(username="demo", role="viewer"))
        assert raised.value.status_code == 403
        assert "viewer" in raised.value.detail

    def test_it_passes_an_operator_through(self) -> None:
        from services.api.auth import require_operator

        principal = Principal(username="duty", role="operator")
        assert require_operator(principal) is principal
