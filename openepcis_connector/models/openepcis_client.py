# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The HTTP client every other model goes through to reach the resolver.

An ``AbstractModel`` rather than a plain module so that it can be overridden by
another addon and stubbed in tests without patching imports:

    self.env["openepcis.client"].get("/sync/channels")

Only ``requests`` is used, which Odoo already depends on — the connector adds no
package of its own, which is what keeps it installable on Odoo Online.
"""

import json
import logging
import time

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

from ..utils.exceptions import OpenepcisError

_logger = logging.getLogger(__name__)

#: (connect, read). The resolver answers master-data calls quickly; the key pool
#: is the slow one and asks for its own timeout — reading GS1's high-water mark
#: means sorting a whole company prefix range upstream.
DEFAULT_TIMEOUT = (5, 30)

#: Methods safe to repeat after a failure that left the outcome unknown. PUT is
#: here deliberately: the catalog treats it as create-or-update, so sending it
#: twice lands the same record. POST is not — it would raise a 409 the second
#: time and, worse, could burn a GS1 key.
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE", "HEAD"})

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 1.5)


class OpenepcisClient(models.AbstractModel):
    _name = "openepcis.client"
    _description = "OpenEPCIS HTTP client"

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @api.model
    def _company(self, company=None):
        return company or self.env.company

    @api.model
    def _settings(self, company=None):
        """Connection settings, or a message naming what is missing."""
        company = self._company(company)
        # sudo: an ordinary user may publish a product without being allowed to
        # read the API secret, which is restricted to system administrators.
        company = company.sudo()
        missing = [
            label
            for field, label in (
                ("openepcis_base_url", _("resolver URL")),
                ("openepcis_api_key", _("API key")),
                ("openepcis_api_secret", _("API secret")),
            )
            if not company[field]
        ]
        if missing:
            raise UserError(
                _(
                    "OpenEPCIS is not configured for %(company)s — missing: %(missing)s.\n"
                    "Settings > General Settings > OpenEPCIS.",
                    company=company.display_name,
                    missing=", ".join(missing),
                )
            )
        return {
            "base_url": company.openepcis_base_url.rstrip("/"),
            "api_key": company.openepcis_api_key,
            "api_secret": company.openepcis_api_secret,
        }

    @api.model
    def is_configured(self, company=None):
        """Whether a call could be attempted at all, without raising."""
        company = self._company(company).sudo()
        return bool(
            company.openepcis_enabled
            and company.openepcis_base_url
            and company.openepcis_api_key
            and company.openepcis_api_secret
        )

    @api.model
    def base_url(self, company=None):
        """Public resolver origin, for building Digital Links."""
        return (self._company(company).sudo().openepcis_base_url or "").rstrip("/")

    # ------------------------------------------------------------------
    # Verbs
    # ------------------------------------------------------------------

    @api.model
    def get(self, path, params=None, **kw):
        return self.request("GET", path, params=params, **kw)

    @api.model
    def put(self, path, payload, **kw):
        return self.request("PUT", path, payload=payload, **kw)

    @api.model
    def post(self, path, payload=None, **kw):
        return self.request("POST", path, payload=payload, **kw)

    @api.model
    def patch(self, path, payload, **kw):
        return self.request("PATCH", path, payload=payload, **kw)

    @api.model
    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    @api.model
    def post_file(self, path, filename, content, form=None, company=None, timeout=None):
        """Upload a file as multipart form data.

        Separate from :meth:`request` because the body is not JSON and must not
        carry a ``Content-Type`` of its own — ``requests`` sets the multipart
        boundary, and overriding it produces an unparseable request.
        """
        settings = self._settings(company)
        url = "%s%s" % (settings["base_url"], path if path.startswith("/") else "/" + path)
        headers = {
            "Accept": "application/json",
            "API-KEY": settings["api_key"],
            "API-KEY-SECRET": settings["api_secret"],
        }
        try:
            response = requests.post(
                url,
                headers=headers,
                files={"file": (filename, content, "application/octet-stream")},
                data=form or {},
                timeout=timeout or (5, 300),
            )
        except requests.exceptions.RequestException as exc:
            raise OpenepcisError(
                _("Could not reach the OpenEPCIS resolver at %s: %s", url, exc), path=path
            ) from exc

        if response.status_code >= 300:
            raise self._error_from(response, path)
        return self._decode(response)

    @api.model
    def request(self, method, path, payload=None, params=None, company=None, timeout=None):
        """Call the resolver and return the decoded body.

        :returns: the parsed JSON, or ``None`` for an empty body (``204``).
        :raises OpenepcisError: for every non-2xx answer and every transport
            failure. Callers decide whether that aborts them or gets recorded.
        """
        settings = self._settings(company)
        url = "%s%s" % (settings["base_url"], path if path.startswith("/") else "/" + path)
        method = method.upper()

        headers = {
            "Accept": "application/json",
            # The resolver's Keycloak mechanism exchanges this pair for a full
            # identity, so roles and claims behave exactly as with a bearer
            # token — but without a refresh cycle to run inside an ERP.
            "API-KEY": settings["api_key"],
            "API-KEY-SECRET": settings["api_secret"],
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"

        attempts = MAX_ATTEMPTS if method in IDEMPOTENT_METHODS else 1
        last_error = None

        for attempt in range(attempts):
            if attempt:
                time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
            try:
                response = requests.request(
                    method,
                    url,
                    headers=headers,
                    params=params or None,
                    data=json.dumps(payload) if payload is not None else None,
                    timeout=timeout or DEFAULT_TIMEOUT,
                )
            except requests.exceptions.RequestException as exc:
                # No status: the request may or may not have been applied. Only
                # idempotent methods reach a second attempt, so repeating is safe.
                last_error = OpenepcisError(
                    _("Could not reach the OpenEPCIS resolver at %s: %s", url, exc),
                    path=path,
                )
                _logger.warning("OpenEPCIS %s %s failed: %s", method, path, exc)
                continue

            if response.status_code < 300:
                return self._decode(response)

            error = self._error_from(response, path)
            if error.is_retryable and attempt < attempts - 1:
                last_error = error
                _logger.info(
                    "OpenEPCIS %s %s answered %s, retrying", method, path, response.status_code
                )
                continue
            raise error

        raise last_error

    # ------------------------------------------------------------------
    # Answers
    # ------------------------------------------------------------------

    @api.model
    def _decode(self, response):
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            # A login page instead of JSON is the classic symptom of an OIDC
            # bounce in front of the API; say so rather than "invalid JSON".
            if "html" in (response.headers.get("Content-Type") or ""):
                raise OpenepcisError(
                    _(
                        "The resolver answered with a web page instead of data — "
                        "the URL probably points at something other than the API."
                    ),
                    status=response.status_code,
                ) from None
            raise OpenepcisError(
                _("The resolver sent a body that is not JSON."), status=response.status_code
            ) from None

    @api.model
    def _error_from(self, response, path):
        """Turn a failed answer into an :class:`OpenepcisError` a human can read.

        The resolver reports errors as RFC 7807 problem documents, so ``detail``
        is already a sentence written for the caller. Prefer it over anything
        this side could invent.
        """
        problem = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                problem = body
        except ValueError:
            pass

        message = problem.get("detail") or problem.get("title") or problem.get("message")
        if not message:
            message = (response.text or "").strip()[:300]
        if not message:
            message = _("The resolver refused the call without saying why.")

        return OpenepcisError(message, status=response.status_code, problem=problem, path=path)

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------

    @api.model
    def diagnose(self, company=None):
        """Probe the connection and report what works, in order of dependency.

        Written as a diagnosis rather than a boolean because every realistic
        failure here is a Keycloak provisioning gap, and each one has a
        different fix. Returns a list of ``(name, ok, detail)``.
        """
        checks = []

        def probe(name, path, params=None, optional=False):
            try:
                self.get(path, params=params, company=company)
            except OpenepcisError as exc:
                # A 404 here is not a configuration problem: the deployment is
                # simply older than the feature. Saying "claim missing" because
                # the endpoint does not exist would send somebody into Keycloak
                # to look for a claim that is already there.
                if exc.status == 404:
                    checks.append(
                        (
                            name,
                            None,
                            _("Not available on this deployment — the resolver predates it."),
                        )
                    )
                    return False
                checks.append((name, False, self._explain(exc, optional)))
                return False
            checks.append((name, True, ""))
            return True

        # Any answer at all proves the URL, the key and the secret.
        if not probe(_("Credentials accepted"), "/products", {"page": 0, "pageSize": 1}):
            return checks

        # /sync/** is the endpoint that insists on the tenant claim, so it is
        # the cheapest way to find out whether defaultGroup made it into the token.
        probe(_("Tenant claim (defaultGroup)"), "/sync/channels")

        # Optional: only tenants that draw their own identifiers have a GS1
        # client and a company prefix, and the rest should not see a red cross.
        probe(_("GS1 company prefix"), "/gs1de/keys", {"ai": "01"}, optional=True)

        return checks

    @api.model
    def _explain(self, error, optional=False):
        """Say what to do about a failed probe, not merely what went wrong."""
        if error.is_missing_claim:
            return _(
                "%(detail)s — add the claim to the Keycloak client's service account "
                "and re-issue the API key.",
                detail=error.message,
            )
        if error.status == 401:
            return _("The API key or secret was rejected.")
        if error.status == 403:
            return _(
                "Authenticated, but the identity lacks the tenant role needed to write. "
                "Grant the realm role named after the tenant."
            )
        if optional and error.status in (400, 404, 409):
            return _("Not configured for this tenant — only needed to draw identifiers.")
        return str(error)
