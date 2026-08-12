# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""Authentication strategies and the discovery that feeds them.

The platform authenticates connectors with an OIDC offline token: a refresh
token issued with the ``offline_access`` scope, exchanged for short-lived
access tokens as needed. Issuing the offline token is a human act in a
browser; this library only consumes one.

Discovery removes the last hand-typed URL besides the resolver's own. The
resolver publishes OAuth 2.0 Protected Resource Metadata (RFC 9728) naming its
authorization server; the authorization server's OIDC discovery document names
the token endpoint. One configured URL, everything else is found.

The seam for a second mode (a customer-supplied GS1 Germany token, or anything
after that) is :class:`AuthStrategy`: one method that yields a bearer, one
that forgets it after a 401.
"""

import base64
import binascii
import json
import time
from typing import Any, Protocol

import requests

from .config import ClientConfig
from .errors import BenelogError

#: Mint a new access token this many seconds before the current one lapses, so
#: a request never starts with a token that expires mid-flight.
EXPIRY_MARGIN = 30

#: Authorization servers discovered from a resolver's protected-resource
#: metadata, keyed by resolver base URL. Class-wide and unbounded on purpose: a
#: process talks to a handful of resolvers, and the answer changes only when a
#: deployment is reconfigured. A restart is an acceptable cache invalidation.
_PROTECTED_RESOURCE: dict[str, str] = {}

#: OIDC discovery documents, keyed by issuer. Same lifetime argument.
_OIDC_CONFIG: dict[str, dict[str, Any]] = {}


class TokenStore(Protocol):
    """Where the host keeps the offline token and what is known about it.

    The host framework owns persistence: an ERP stores the token on a company
    record, a script keeps it in memory or a secret store. Two of the three
    methods exist because of Keycloak behaviour the library must honour:
    rotation ("Revoke Refresh Token") replaces the offline token on every
    exchange, and losing the replacement locks the connector out with a
    credential that still looks correct on screen.
    """

    def get_offline_token(self) -> str:
        """The current offline token. Read on every mint, so rotation holds."""
        ...

    def save_offline_token(self, token: str) -> None:
        """Persist a rotated offline token the moment it arrives."""
        ...

    def save_subject(self, username: str) -> None:
        """Record whom the token belongs to, for display next to the setting."""
        ...


class AuthStrategy(Protocol):
    """A source of bearer tokens for :class:`~benelog_client.core.client.Client`."""

    def bearer(self) -> str:
        """A currently valid access token."""
        ...

    def invalidate(self) -> None:
        """Forget any cached token; the next :meth:`bearer` mints afresh."""
        ...


class InMemoryTokenStore:
    """A token store for scripts and tests. Nothing outlives the process."""

    def __init__(self, offline_token: str) -> None:
        self.offline_token = offline_token
        self.subject = ""

    def get_offline_token(self) -> str:
        return self.offline_token

    def save_offline_token(self, token: str) -> None:
        self.offline_token = token

    def save_subject(self, username: str) -> None:
        self.subject = username


def token_claims(token: str) -> dict[str, Any]:
    """The claims of a JWT, read without verifying it.

    Only ever used to describe a token back to whoever deposited it. Nothing is
    authorised on this basis; the platform and Keycloak do the verifying.
    Answers an empty dict for anything opaque.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def token_type(token: str) -> str:
    """The ``typ`` claim: ``Offline`` for the token this library expects."""
    return str(token_claims(token).get("typ") or "")


def token_subject(token: str) -> str:
    """Whom a token belongs to, as well as it can be told."""
    claims = token_claims(token)
    return str(claims.get("preferred_username") or claims.get("sub") or "")


class OfflineTokenAuth:
    """Mode one: benelog credentials via an OIDC offline token.

    :param config: where the resolver is and how patient to be.
    :param token_store: the host's persistence for the offline token.
    :param client_id: the OIDC client this connector authenticates as.
    :param client_secret: only for a confidential client.
    :param issuer_override: skip RFC 9728 discovery and use this realm URL.
        For a resolver that publishes no protected-resource metadata.
    :param session: a ``requests``-compatible session, injectable for tests.
    """

    def __init__(
        self,
        config: ClientConfig,
        token_store: TokenStore,
        client_id: str,
        client_secret: str = "",
        issuer_override: str = "",
        session: requests.Session | None = None,
    ) -> None:
        self._config = config
        self._store = token_store
        self._client_id = client_id
        self._client_secret = client_secret
        self._issuer_override = issuer_override.rstrip("/")
        self._session = session or requests.Session()
        self._access_token = ""
        self._expires_at = 0.0

    # -- AuthStrategy ------------------------------------------------------

    def bearer(self) -> str:
        if self._access_token and self._expires_at > time.time():
            return self._access_token
        token, lifetime = self._mint()
        self._access_token = token
        self._expires_at = time.time() + max(lifetime - EXPIRY_MARGIN, 5)
        return token

    def invalidate(self) -> None:
        self._access_token = ""
        self._expires_at = 0.0

    # -- Discovery ---------------------------------------------------------

    def issuer(self) -> str:
        """The authorization server, configured or discovered (RFC 9728)."""
        if self._issuer_override:
            return self._issuer_override
        base_url = self._config.base_url
        if base_url in _PROTECTED_RESOURCE:
            return _PROTECTED_RESOURCE[base_url]

        url = f"{base_url}/.well-known/oauth-protected-resource"
        try:
            response = self._session.get(url, timeout=self._config.token_timeout)
        except requests.exceptions.RequestException as exc:
            raise BenelogError(f"Could not reach the resolver at {url}: {exc}") from exc
        if response.status_code >= 300:
            raise BenelogError(
                f"The resolver at {base_url} does not publish OAuth metadata "
                f"({url} answered {response.status_code}). Configure the realm "
                "URL explicitly.",
                status=response.status_code,
            )
        try:
            servers = (response.json() or {}).get("authorization_servers") or []
        except ValueError as exc:
            raise BenelogError(
                f"The resolver at {url} returned no OAuth metadata document."
            ) from exc
        if not servers:
            raise BenelogError(
                f"The resolver at {url} names no authorization server in its OAuth metadata."
            )

        issuer = str(servers[0]).rstrip("/")
        _PROTECTED_RESOURCE[base_url] = issuer
        return issuer

    def _oidc_config(self) -> dict[str, Any]:
        """The realm's discovery document, so no endpoint is hardcoded."""
        issuer = self.issuer()
        if issuer in _OIDC_CONFIG:
            return _OIDC_CONFIG[issuer]

        url = f"{issuer}/.well-known/openid-configuration"
        try:
            response = self._session.get(url, timeout=self._config.token_timeout)
        except requests.exceptions.RequestException as exc:
            raise BenelogError(f"Could not reach the realm at {url}: {exc}") from exc
        if response.status_code >= 300:
            raise BenelogError(
                f"The realm URL does not look like an OIDC realm: {url} answered "
                f"{response.status_code}. It should end in /realms/<name>.",
                status=response.status_code,
            )
        try:
            document = response.json()
        except ValueError as exc:
            raise BenelogError(f"The realm at {url} did not return a discovery document.") from exc
        if not isinstance(document, dict):
            raise BenelogError(f"The realm at {url} did not return a discovery document.")

        _OIDC_CONFIG[issuer] = document
        return document

    # -- Token exchange ------------------------------------------------------

    def _mint(self) -> tuple[str, int]:
        """Exchange the offline token for an access token.

        Honours rotation: when the realm has "Revoke Refresh Token" switched
        on, Keycloak replaces the offline token on every exchange, and the
        replacement goes straight to the :class:`TokenStore`. Failing to store
        it would lock the connector out at the next refresh with a credential
        that still looks unchanged where it was pasted.
        """
        endpoint = self._oidc_config().get("token_endpoint")
        if not endpoint:
            raise BenelogError("The realm advertises no token endpoint.")

        offline_token = self._store.get_offline_token()
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": offline_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            payload["client_secret"] = self._client_secret

        try:
            response = self._session.post(
                str(endpoint), data=payload, timeout=self._config.token_timeout
            )
        except requests.exceptions.RequestException as exc:
            raise BenelogError(f"Could not reach the token endpoint: {exc}") from exc

        if response.status_code >= 300:
            raise self._token_error(response)

        body = response.json()
        access_token = body.get("access_token")
        if not access_token:
            raise BenelogError("The token endpoint returned no access token.")

        rotated = body.get("refresh_token")
        if rotated and rotated != offline_token:
            self._store.save_offline_token(str(rotated))

        # An offline refresh token carries no preferred_username, so a token
        # deposited by hand can only be labelled with its subject UUID. The
        # access token does carry it; correct the label the first time one is
        # minted, which is when the information becomes available.
        username = token_claims(str(access_token)).get("preferred_username")
        if username:
            self._store.save_subject(str(username))

        return str(access_token), int(body.get("expires_in") or 60)

    @staticmethod
    def _token_error(response: requests.Response) -> BenelogError:
        """Phrase a token failure as something an operator can act on."""
        detail: dict[str, Any] = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = body
        except ValueError:
            pass
        code = str(detail.get("error") or "")
        description = str(detail.get("error_description") or "")

        if code == "invalid_grant":
            # A token is bound to the issuer URL it was minted under. Where two
            # hostnames serve the same realm, a token issued via one and
            # refreshed via the other fails here; calling that "revoked" sends
            # the reader to the wrong fix. Keycloak names the expected issuer,
            # so pass it through.
            if "issuer" in description.lower():
                return BenelogError(
                    "The offline token was issued by a different URL than the "
                    f"one configured here: {description}. A token is bound to "
                    "the issuer it was minted under; an alias hostname for the "
                    "same realm counts as different.",
                    status=response.status_code,
                    problem=detail,
                )
            return BenelogError(
                f"The offline token is no longer accepted ({description or code}). "
                "It has been revoked, or the realm's offline session has been "
                "removed. Deposit a fresh one.",
                status=response.status_code,
                problem=detail,
            )
        if code == "unauthorized_client":
            return BenelogError(
                "The identity provider refused the client. Check the client ID, "
                "and the secret if the client is confidential.",
                status=response.status_code,
                problem=detail,
            )
        return BenelogError(
            "The identity provider refused to issue a token: "
            f"{description or code or response.text[:200]}",
            status=response.status_code,
            problem=detail,
        )
