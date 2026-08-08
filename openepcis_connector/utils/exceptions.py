# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The one exception the HTTP client raises, carrying enough to act on.

Kept apart from Odoo's ``UserError`` on purpose. Background work (the cron that
drains the sync queue) has to *record* a failure and carry on; only interactive
callers turn it into a dialog. An exception that is already a ``UserError``
tempts every layer to re-raise it and abort a batch over one bad record.
"""


class OpenepcisError(Exception):
    """A call to the OpenEPCIS resolver did not succeed.

    :param message: already phrased for a human — the RFC 7807 ``detail`` when
        the server sent one, otherwise a description of the transport failure.
    :param status: HTTP status, or ``None`` when the request never got an answer.
    :param problem: the parsed RFC 7807 body, when there was one.
    """

    def __init__(self, message, status=None, problem=None, path=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.problem = problem or {}
        self.path = path

    @property
    def is_auth_error(self):
        """Credentials rejected, or the token lacks the role the write needs."""
        return self.status in (401, 403)

    @property
    def is_conflict(self):
        """The record already exists — what ``POST`` answers for a known key."""
        return self.status == 409

    @property
    def is_missing_claim(self):
        """A 400 that means the token is short a claim rather than the body being wrong.

        The resolver answers ``400`` with a plain sentence when ``defaultGroup``
        or ``gs1CompanyPrefix`` is absent from the identity. That is a
        provisioning problem in Keycloak, not something the user can fix by
        editing the record, so it deserves its own message.
        """
        return self.status == 400 and "carries no" in (self.message or "")

    @property
    def is_retryable(self):
        """Worth trying again later: transport trouble or a gateway wobble."""
        return self.status is None or self.status in (429, 502, 503, 504)

    def __str__(self):
        if self.status:
            return "%s (HTTP %s)" % (self.message, self.status)
        return self.message
