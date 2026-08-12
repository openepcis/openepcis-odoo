# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""The GS1 registry side of the platform: keys, channels, credentials, upstream.

Four concerns share this module because they share one story — what connects a
record in an ERP to the GS1 world:

- **Key pool**: drawing a GTIN or GLN from the tenant's licensed range,
  confirming it once the record using it is saved, releasing it when not.
  Stateful and hard to reverse, so the calls here stay deliberately thin and
  the reserve-then-commit choreography belongs to the host.
- **Channels**: which downstream targets exist and what terms they require.
- **Credentials**: depositing a customer's own GS1 Germany token with the
  platform. Paste, deposit, forget: the platform validates the token against
  GS1 before storing anything, keeps it encrypted, and discovers the licensed
  prefixes from GS1's own answer. The host never persists the token.
- **Upstream sync**: reading and triggering onward publication per record,
  including Verified by GS1.
"""

from dataclasses import dataclass, field
from typing import Any

from ..core.client import Client

#: Drawing a key walks the tenant's range server-side, which takes longer than
#: an ordinary call; the addon measured it and settled on this.
DRAW_TIMEOUT: tuple[float, float] = (5, 90)


@dataclass(frozen=True)
class Channel:
    """One downstream sync target, as ``GET /sync/channels`` describes it."""

    id: str
    display_name: str
    enabled: bool
    dry_run: bool
    configured: bool
    required_terms: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Terms the channel refuses a record without, keyed by record kind
    (``PRODUCT``, ``ORGANIZATION``), values prefixed ``gs1:``."""


@dataclass(frozen=True)
class CredentialSlot:
    """One deposited GS1 Germany credential, secrets masked by the platform."""

    label: str
    """The slot the credential is addressed by; ``default`` unless named."""

    token_status: str
    """The platform's last validation verdict, e.g. ``VALID``."""

    licence_type: str
    allowed_gcps: tuple[str, ...]
    """The prefixes GS1 licenses to this token — read from GS1's own
    ``user/info``, never typed in."""

    primary_gcp: str
    validated_at: str
    raw: dict[str, Any] = field(default_factory=dict)
    """The full masked descriptor, for fields this projection does not name."""


@dataclass(frozen=True)
class Verification:
    """What Verified by GS1 answered for one key."""

    key: str
    verified: bool
    type: str
    licensee_name: str
    licensee_gln: str
    licence_key: str
    licence_type: str


class Registry:
    """Registry calls against the resolver, through a configured client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    # -- Key pool ------------------------------------------------------------

    def draw_key(self, ai: str) -> str:
        """Reserve the next free key for an anchor AI (``01``, ``414``, ``417``).

        Non-idempotent and single-attempt by design: a retried draw would burn
        a second number from the licence. The key is held, not yet registered
        with GS1; :meth:`confirm_key` when the record using it is saved,
        :meth:`release_key` when it is not.

        :raises BenelogError: with :attr:`~.BenelogError.is_conflict` when the
            tenant holds no licence for the AI, and
            :attr:`~.BenelogError.is_missing_claim` when the identity carries
            no company prefix.
        """
        answer = self._client.request(
            "POST", "/gs1de/keys/draw", payload={"ai": ai}, timeout=DRAW_TIMEOUT
        )
        return str(answer["key"])

    def confirm_key(self, ai: str, key: str) -> None:
        """Register a held key with GS1; call when the record using it is saved."""
        self._client.post(f"/gs1de/keys/{ai}/{key}/confirm")

    def release_key(self, ai: str, key: str) -> None:
        """Hand a held key back to the pool."""
        self._client.delete(f"/gs1de/keys/{ai}/{key}")

    # -- Channels ------------------------------------------------------------

    def channels(self) -> list[Channel]:
        """The downstream sync targets and what each requires."""
        return [
            Channel(
                id=str(entry.get("id")),
                display_name=str(entry.get("displayName") or entry.get("id")),
                enabled=bool(entry.get("enabled")),
                dry_run=bool(entry.get("dryRun")),
                configured=bool(entry.get("configured")),
                required_terms={
                    str(kind): tuple(str(term) for term in terms or ())
                    for kind, terms in (entry.get("requiredTerms") or {}).items()
                },
            )
            for entry in (self._client.get("/sync/channels") or [])
            if entry.get("id")
        ]

    # -- GS1 Germany credentials ----------------------------------------------

    def deposit_gs1_credential(
        self,
        auth_token: str,
        label: str = "",
        licence_key: str = "",
        licence_type: str = "",
    ) -> CredentialSlot:
        """Deposit or replace a GS1 Germany token with the platform.

        The platform validates the token against GS1 **before** storing
        anything; an unusable token comes back as a 400 with the reason and
        nothing is written. On success the answer carries the licensed
        prefixes, so the host can show what the deposit enables — and then
        forget the token, which the platform now holds encrypted.
        """
        payload: dict[str, Any] = {"authToken": auth_token}
        if licence_key:
            payload["licenceKey"] = licence_key
        if licence_type:
            payload["licenceType"] = licence_type
        answer = self._client.put(self._credential_path(label), payload)
        return self._slot(answer.get("label") or label or "default", answer.get("credential"))

    def gs1_credentials(self) -> list[CredentialSlot]:
        """All deposited credentials, tokens masked. Empty is a normal answer."""
        return [
            self._slot(str(entry.get("label") or "default"), entry.get("credential"))
            for entry in (self._client.get("/gs1de/my-credentials") or [])
        ]

    def revalidate_gs1_credential(self, label: str = "") -> CredentialSlot:
        """Re-check a stored token against GS1, e.g. after GS1 rotated it."""
        path = self._credential_path(label) + "/validate"
        # Unlike deposit and list, validate answers the bare masked
        # descriptor without a label wrapper.
        answer = self._client.post(path)
        return self._slot(label or "default", answer)

    def withdraw_gs1_credential(self, label: str = "") -> None:
        """Remove a deposited credential and everything that advertised it."""
        self._client.delete(self._credential_path(label))

    @staticmethod
    def _credential_path(label: str) -> str:
        return f"/gs1de/my-credentials/{label}" if label else "/gs1de/my-credentials"

    @staticmethod
    def _slot(label: str, credential: Any) -> CredentialSlot:
        masked = credential if isinstance(credential, dict) else {}
        return CredentialSlot(
            label=label,
            token_status=str(masked.get("tokenStatus") or ""),
            licence_type=str(masked.get("licenceType") or ""),
            allowed_gcps=tuple(str(gcp) for gcp in masked.get("allowedGcps") or ()),
            primary_gcp=str(masked.get("primaryGcp") or ""),
            validated_at=str(masked.get("tokenValidatedAt") or ""),
            raw=masked,
        )

    # -- Verified by GS1 -------------------------------------------------------

    def verified_by_gs1(self, key: str) -> Verification:
        """Ask whether GS1 knows a key and who licenses it. Nothing is stored.

        Authenticated only: the platform answers with the caller's own
        deposited GS1 token where there is one, falling back to the platform
        credential. Which of the two answered is not visible here.
        """
        answer = self._client.get(f"/masterdata/verify/{key}") or {}
        return Verification(
            key=str(answer.get("key") or key),
            verified=bool(answer.get("verified")),
            type=str(answer.get("type") or ""),
            licensee_name=str(answer.get("licenseeName") or ""),
            licensee_gln=str(answer.get("licenseeGln") or ""),
            licence_key=str(answer.get("licenceKey") or ""),
            licence_type=str(answer.get("licenceType") or ""),
        )

    # -- Upstream sync ---------------------------------------------------------

    def sync_status(self, ai: str, key: str) -> Any:
        """The record's standing per channel, as the platform reports it."""
        return self._client.get(f"/sync/status/{ai}/{key}")

    def sync_history(self, ai: str, key: str, channel: str = "", size: int = 20) -> list[Any]:
        """Past sync operations for one record, newest first."""
        params: dict[str, Any] = {"size": size}
        if channel:
            params["channel"] = channel
        return list(self._client.get(f"/sync/history/{ai}/{key}", params=params) or [])

    def trigger_sync(self, ai: str, key: str, channel: str, mode: str = "MANUAL") -> Any:
        """Push one record to a channel now (``MANUAL``) or retry it (``RETRY``).

        Not idempotent — a trigger enqueues work — so it never retries; the
        answer reports whether the channel accepted, skipped or refused it.
        """
        return self._client.post(f"/sync/trigger/{ai}/{key}", {"channel": channel, "mode": mode})
