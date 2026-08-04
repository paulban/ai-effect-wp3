"""Signed, expiring links that authorise access to a session.

A participant reaches their simulator and questionnaire through URLs this
service hands out. Before this module, those URLs were host ports — anyone who
found port 8090 was inside a running study, and there was nothing to tell one
participant's session from another's.

Each session link now carries a token bound to that session id and an expiry.
The proxy verifies it before a request reaches any session container, and the
collect endpoints verify it before accepting results, because the producers
that post results run in the participant's browser and cannot hold the service
API key.

The scheme is a keyed MAC rather than an encrypted or database-backed token on
purpose: verification needs no round trip and no shared state, so the proxy can
reject a forged link without consulting this service at all.

Tokens are `v1.<expiry-epoch-seconds>.<url-safe-base64 signature>`, with the
version prefix so the scheme can be replaced without ambiguity.

Spec coverage: FR-04, FR-15, FR-16
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Environment variable holding the signing secret. Required: a default would be
# a published secret, and every deployment sharing it would accept every other
# deployment's links.
TOKEN_SECRET_ENVIRONMENT_VARIABLE = "HAI_SESSION_TOKEN_SECRET"

# Minimum acceptable secret length in bytes. 32 bytes matches the output of the
# `openssl rand -hex 32` the project README already recommends for API keys.
MINIMUM_SECRET_LENGTH = 32

# How long a freshly issued session link stays valid, in seconds. Sessions are
# scheduled appointments lasting under an hour, so a link that outlives the
# working day is a liability rather than a convenience.
DEFAULT_TOKEN_LIFETIME_SECONDS = 4 * 60 * 60

# Current token scheme version.
TOKEN_VERSION = "v1"


@dataclass(frozen=True)
class TokenVerification:
    """Outcome of checking a token against a session id.

    Attributes:
        is_valid: True only when the token is well-formed, correctly signed for
            this session, and unexpired.
        reason: Machine-readable failure reason — "malformed", "bad_signature",
            "expired" or "wrong_session". Empty when valid. Deliberately coarse:
            it goes into logs, never into a response body, because telling a
            caller *why* their forgery failed helps them forge better.
    """

    is_valid: bool
    reason: str = ""


class MissingTokenSecretError(RuntimeError):
    """Raised when the signing secret is absent or too short to be safe."""


def load_token_secret() -> bytes:
    """
    Read and validate the signing secret from the environment.

    Read on every call rather than cached at import so that a test can set the
    variable after importing the module, and so a deployment that forgot to set
    it fails at the first request with a clear error instead of at import with a
    stack trace from somewhere unrelated.

    Returns:
        The secret as bytes.

    Raises:
        MissingTokenSecretError: If the variable is unset, empty, or shorter
            than MINIMUM_SECRET_LENGTH characters.
    """
    secret = os.environ.get(TOKEN_SECRET_ENVIRONMENT_VARIABLE, "")

    if not secret:
        raise MissingTokenSecretError(
            f"{TOKEN_SECRET_ENVIRONMENT_VARIABLE} is not set. "
            "Generate one with `openssl rand -hex 32` and pass it to the service; "
            "session links cannot be issued or verified without it."
        )

    if len(secret) < MINIMUM_SECRET_LENGTH:
        raise MissingTokenSecretError(
            f"{TOKEN_SECRET_ENVIRONMENT_VARIABLE} must be at least "
            f"{MINIMUM_SECRET_LENGTH} characters, got {len(secret)}."
        )

    return secret.encode("utf-8")


def _compute_signature(session_id: str, expires_at: int, secret: bytes) -> str:
    """
    Compute the MAC binding a session id to an expiry.

    Both fields are inside the signed message, so a token cannot be moved to a
    different session or have its expiry extended without invalidating it.

    Args:
        session_id: Session the token authorises.
        expires_at: Expiry as a Unix timestamp in seconds.
        secret: Signing secret.

    Returns:
        URL-safe base64 signature without padding, so it survives being placed
        in a URL path or query string unescaped.
    """
    signed_message = f"{TOKEN_VERSION}.{session_id}.{expires_at}".encode("utf-8")
    digest = hmac.new(secret, signed_message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def issue_token(
    session_id: str,
    lifetime_seconds: int = DEFAULT_TOKEN_LIFETIME_SECONDS,
) -> str:
    """
    Issue a signed token authorising access to one session.

    Args:
        session_id: Session the token authorises.
        lifetime_seconds: Validity window from now. Must be positive.

    Returns:
        The token, to be appended to a session URL.

    Raises:
        ValueError: If session_id is empty or lifetime_seconds is not positive.
        MissingTokenSecretError: If the signing secret is unavailable.

    Example:
        >>> os.environ["HAI_SESSION_TOKEN_SECRET"] = "x" * 32
        >>> token = issue_token("session-abc", lifetime_seconds=60)
        >>> verify_token("session-abc", token).is_valid
        True
    """
    if not session_id:
        raise ValueError("session_id must not be empty")
    if lifetime_seconds <= 0:
        raise ValueError(f"lifetime_seconds must be positive, got {lifetime_seconds}")

    secret = load_token_secret()
    expires_at = int(time.time()) + lifetime_seconds
    signature = _compute_signature(session_id, expires_at, secret)

    return f"{TOKEN_VERSION}.{expires_at}.{signature}"


def verify_token(session_id: str, token: str) -> TokenVerification:
    """
    Check a token against the session it claims to authorise.

    Never raises on malformed input: this runs on the request path for
    unauthenticated callers, so every failure mode has to be an ordinary
    rejection rather than a 500.

    Args:
        session_id: Session the caller is trying to reach.
        token: Token supplied by the caller.

    Returns:
        A TokenVerification. Inspect `is_valid`; `reason` is for logging.
    """
    if not token or not session_id:
        return TokenVerification(is_valid=False, reason="malformed")

    token_parts = token.split(".")
    if len(token_parts) != 3 or token_parts[0] != TOKEN_VERSION:
        return TokenVerification(is_valid=False, reason="malformed")

    _, raw_expiry, presented_signature = token_parts

    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return TokenVerification(is_valid=False, reason="malformed")

    try:
        secret = load_token_secret()
    except MissingTokenSecretError:
        # A deployment without a secret must reject every link rather than
        # accept any. Logged at error level because it is a misconfiguration,
        # not a caller mistake.
        logger.error("Session token verification attempted without a signing secret")
        return TokenVerification(is_valid=False, reason="bad_signature")

    expected_signature = _compute_signature(session_id, expires_at, secret)

    # Constant-time comparison: a short-circuiting comparison leaks how many
    # leading bytes of a guess were correct, which is enough to forge a token
    # one byte at a time.
    if not hmac.compare_digest(expected_signature, presented_signature):
        return TokenVerification(is_valid=False, reason="bad_signature")

    # Compared against the unrounded clock: truncating the current time to a
    # whole second here would keep a token valid for up to a second past its
    # own expiry, because the expiry itself was computed from a truncated
    # issue time.
    if expires_at < time.time():
        return TokenVerification(is_valid=False, reason="expired")

    return TokenVerification(is_valid=True)


def build_session_url(base_url: str, session_id: str, tool_path: str, token: str) -> str:
    """
    Build the URL handed to a participant for one session tool.

    The token travels as a query parameter because that is the only place a
    plain browser navigation can carry it. The proxy is expected to exchange it
    for a cookie on first use and redirect to the clean path, so the token does
    not persist in access logs, browser history or referrer headers (FR-16).

    Args:
        base_url: Public base URL of the node, without a trailing slash.
        session_id: Session identifier.
        tool_path: Which tool the URL addresses — "gui" or "survey".
        token: Token issued for this session.

    Returns:
        Absolute URL for the participant.

    Example:
        >>> build_session_url("https://node.example.org", "abc", "gui", "v1.1.sig")
        'https://node.example.org/s/abc/gui?t=v1.1.sig'
    """
    return f"{base_url.rstrip('/')}/s/{session_id}/{tool_path}?t={token}"
