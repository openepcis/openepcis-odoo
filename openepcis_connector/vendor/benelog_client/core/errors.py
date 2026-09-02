# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""The one exception the HTTP client raises, carrying enough to act on.

Deliberately a plain exception rather than a host framework's user-facing
error. Background work (a queue drain) has to *record* a failure and carry on;
only interactive callers turn it into a dialog. An exception that already is a
dialog tempts every layer to re-raise it and abort a batch over one bad record.

The message is a plain English description with the structured facts alongside
it. Host adapters phrase and translate; this library never produces text meant
for an end user's screen.
"""


class BenelogError(Exception):
    """A call to the benelog platform did not succeed.

    :param message: a plain description. The RFC 7807 ``detail`` when the
        server sent one, otherwise what went wrong in transport.
    :param status: HTTP status, or ``None`` when the request never got an
        answer.
    :param problem: the parsed RFC 7807 body, when there was one.
    :param path: the request path, for logs.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        problem: dict[str, object] | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.problem: dict[str, object] = problem or {}
        self.path = path

    @property
    def is_auth_error(self) -> bool:
        """Credentials rejected, or the token lacks the role the write needs."""
        return self.status in (401, 403)

    @property
    def is_conflict(self) -> bool:
        """The record already exists: what ``POST`` answers for a known key."""
        return self.status == 409

    @property
    def is_missing_claim(self) -> bool:
        """A 400 that means the token is short a claim, so the body is fine.

        The resolver answers ``400`` with a plain sentence when ``defaultGroup``
        or ``gs1CompanyPrefix`` is absent from the identity. That is a
        provisioning problem in Keycloak, and no edit to the record fixes it, so
        it deserves its own message in the host.

        The substring match is a known fragility: the platform publishes no
        error code for this case, so the message is all there is to go on.
        Replace it with a code the moment one exists.
        """
        return self.status == 400 and "carries no" in (self.message or "")

    @property
    def is_retryable(self) -> bool:
        """Worth trying again later: transport trouble or a gateway wobble."""
        return self.status is None or self.status in (429, 502, 503, 504)

    def __str__(self) -> str:
        if self.status:
            return f"{self.message} (HTTP {self.status})"
        return self.message
