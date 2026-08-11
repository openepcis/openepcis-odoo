# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The HTTP client every other model goes through to reach the resolver.

An ``AbstractModel`` rather than a plain module so that it can be overridden by
another addon and stubbed in tests without patching imports:

    self.env["openepcis.client"].get("/sync/channels")

**Authentication is an OIDC offline token.** What Odoo stores is a refresh token
issued with the ``offline_access`` scope; every call carries a short-lived access
token minted from it. The alternative — a static key and secret sent with each
request — puts a credential that never expires on the wire every time, and it
cannot be told apart from any other holder of the same string. An offline token
is bound to a subject, yields access tokens that expire in minutes, and can be
revoked centrally in Keycloak without touching Odoo.

**This module only ever consumes such a token; it never mints one.** Issuing an
offline token belongs where a human is present in a browser — the platform's own
web interface, which already runs an authorization-code flow with consent.
Minting it here would instead need Keycloak's deprecated password grant enabled
for the whole realm, and would route a user's password through the ERP. So the
token is deposited by an administrator, and this module exchanges it.

The practical consequence for whoever sets this up is that the claims the
resolver insists on (``defaultGroup``, ``gs1CompanyPrefix``, the tenant role)
come from the user the token was issued for. There is no service account to
provision separately.

Only ``requests`` is used, which Odoo already depends on — the connector adds no
package of its own, which is what keeps it installable on Odoo Online.
"""

import base64
import binascii
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

#: Talking to Keycloak should be quick or not at all; a hung token endpoint must
#: not hold a cron worker.
TOKEN_TIMEOUT = (5, 15)

#: Methods safe to repeat after a failure that left the outcome unknown. PUT is
#: here deliberately: the catalog treats it as create-or-update, so sending it
#: twice lands the same record. POST is not — it would raise a 409 the second
#: time and, worse, could burn a GS1 key.
IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE", "HEAD"})

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 1.5)

#: Mint a new access token this many seconds before the current one lapses, so a
#: request never starts with a token that expires mid-flight.
EXPIRY_MARGIN = 30

#: Access tokens, keyed by (database, company). Per worker and deliberately not
#: in the database: an access token lives minutes, and writing one on every
#: refresh would put a short-lived secret into the table and the audit log for no
#: gain. A restart costs one extra token request.
_ACCESS_TOKENS = {}

#: Discovery documents, keyed by issuer. These change when a realm is
#: reconfigured, which is rare enough that a restart is an acceptable cache
#: invalidation.
_OIDC_CONFIG = {}

#: Authorization server (Keycloak realm) discovered from a resolver's OAuth 2.0
#: Protected Resource Metadata (RFC 9728), keyed by resolver base URL. Same
#: rationale and lifetime as _OIDC_CONFIG: it changes only when the deployment is
#: reconfigured, so a restart is an acceptable cache invalidation.
_PROTECTED_RESOURCE = {}


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
        # read the offline token, which is restricted to administrators.
        company = company.sudo()
        # The realm URL is no longer required: it is discovered from the resolver
        # itself (RFC 9728, see _discover_issuer). openepcis_oidc_issuer stays as
        # an optional override for a deployment that does not publish the metadata.
        missing = [
            label
            for field, label in (
                ("openepcis_base_url", _("resolver URL")),
                ("openepcis_client_id", _("client ID")),
                ("openepcis_offline_token", _("offline token")),
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
        base_url = company.openepcis_base_url.rstrip("/")
        override = (company.openepcis_oidc_issuer or "").rstrip("/")
        return {
            "base_url": base_url,
            "issuer": override or self._discover_issuer(base_url),
            "client_id": company.openepcis_client_id,
            "client_secret": company.openepcis_client_secret or "",
            "offline_token": company.openepcis_offline_token,
        }

    @api.model
    def is_configured(self, company=None):
        """Whether a call could be attempted at all, without raising."""
        company = self._company(company).sudo()
        return bool(
            company.openepcis_enabled
            and company.openepcis_base_url
            and company.openepcis_client_id
            and company.openepcis_offline_token
        )

    @api.model
    def base_url(self, company=None):
        """Public resolver origin, for building Digital Links."""
        return (self._company(company).sudo().openepcis_base_url or "").rstrip("/")

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    @api.model
    def _discover_issuer(self, base_url):
        """The authorization server (Keycloak realm) the resolver trusts.

        RFC 9728 (OAuth 2.0 Protected Resource Metadata): the resolver publishes
        ``/.well-known/oauth-protected-resource`` naming its
        ``authorization_servers``. Reading it means an administrator configures
        ONE URL — the resolver's — and the realm is discovered, instead of
        pasting a realm URL that has to match what the resolver actually accepts
        (the class of mistake _token_error spends most of its lines untangling).

        Cached per resolver per worker, like the OIDC discovery document.
        ``openepcis_oidc_issuer`` remains an override for a deployment that does
        not publish the metadata.
        """
        if base_url in _PROTECTED_RESOURCE:
            return _PROTECTED_RESOURCE[base_url]

        url = "%s/.well-known/oauth-protected-resource" % base_url
        try:
            response = requests.get(url, timeout=TOKEN_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise OpenepcisError(
                _("Could not reach the resolver at %(url)s: %(why)s", url=url, why=exc)
            ) from exc
        if response.status_code >= 300:
            raise OpenepcisError(
                _(
                    "The resolver at %(base)s does not publish OAuth metadata "
                    "(%(url)s answered %(status)s). Set the Keycloak realm URL "
                    "manually under Settings > OpenEPCIS.",
                    base=base_url,
                    url=url,
                    status=response.status_code,
                ),
                status=response.status_code,
            )
        try:
            servers = (response.json() or {}).get("authorization_servers") or []
        except ValueError as exc:
            raise OpenepcisError(
                _("The resolver at %s returned no OAuth metadata document.", url)
            ) from exc
        if not servers:
            raise OpenepcisError(
                _(
                    "The resolver at %s names no authorization server in its "
                    "OAuth metadata.",
                    url,
                )
            )

        issuer = servers[0].rstrip("/")
        _PROTECTED_RESOURCE[base_url] = issuer
        return issuer

    @api.model
    def _oidc_config(self, issuer):
        """The realm's discovery document, so no endpoint is hardcoded.

        Keycloak's token endpoint is predictable, but deployments move behind
        different hostnames and the platform already uses more than one. Asking
        the realm where its endpoints are costs one request per worker and
        removes a class of configuration mistake.
        """
        if issuer in _OIDC_CONFIG:
            return _OIDC_CONFIG[issuer]

        url = "%s/.well-known/openid-configuration" % issuer
        try:
            response = requests.get(url, timeout=TOKEN_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise OpenepcisError(
                _("Could not reach the Keycloak realm at %(url)s: %(why)s", url=url, why=exc)
            ) from exc
        if response.status_code >= 300:
            raise OpenepcisError(
                _(
                    "The realm URL does not look like a Keycloak realm — "
                    "%(url)s answered %(status)s. It should end in "
                    "/realms/<name>.",
                    url=url,
                    status=response.status_code,
                ),
                status=response.status_code,
            )
        try:
            config = response.json()
        except ValueError as exc:
            raise OpenepcisError(
                _("The realm at %s did not return a discovery document.", url)
            ) from exc

        _OIDC_CONFIG[issuer] = config
        return config

    @api.model
    def _cache_key(self, company):
        return (self.env.cr.dbname, company.id)

    @api.model
    def _access_token(self, company, force=False):
        """A usable access token, minted from the offline token when needed."""
        key = self._cache_key(company)
        if not force:
            cached = _ACCESS_TOKENS.get(key)
            if cached and cached[1] > time.time():
                return cached[0]

        settings = self._settings(company)
        token, lifetime = self._mint_access_token(settings, company)
        _ACCESS_TOKENS[key] = (token, time.time() + max(lifetime - EXPIRY_MARGIN, 5))
        return token

    @api.model
    def _mint_access_token(self, settings, company):
        """Exchange the offline token for an access token."""
        endpoint = self._oidc_config(settings["issuer"]).get("token_endpoint")
        if not endpoint:
            raise OpenepcisError(_("The realm advertises no token endpoint."))

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": settings["offline_token"],
            "client_id": settings["client_id"],
        }
        if settings["client_secret"]:
            payload["client_secret"] = settings["client_secret"]

        try:
            response = requests.post(endpoint, data=payload, timeout=TOKEN_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            raise OpenepcisError(_("Could not reach the token endpoint: %s", exc)) from exc

        if response.status_code >= 300:
            raise self._token_error(response)

        body = response.json()
        access_token = body.get("access_token")
        if not access_token:
            raise OpenepcisError(_("The token endpoint returned no access token."))

        # Keycloak rotates the refresh token when "Revoke Refresh Token" is on.
        # Failing to store the new one would lock the connector out at the next
        # refresh, with a credential that looks unchanged in the settings.
        rotated = body.get("refresh_token")
        if rotated and rotated != settings["offline_token"]:
            company.sudo().with_context(openepcis_syncing=True).write(
                {"openepcis_offline_token": rotated}
            )
            settings["offline_token"] = rotated
            _logger.info("OpenEPCIS: offline token rotated by the identity provider")

        # An offline refresh token carries no preferred_username, so a token
        # deposited by hand could only be labelled with its subject UUID. The
        # access token does carry it, so the label is corrected the first time
        # one is minted — which is when the information becomes available.
        username = self._token_claims(access_token).get("preferred_username")
        if username and company.sudo().openepcis_token_subject != username:
            company.sudo().with_context(openepcis_syncing=True).write(
                {"openepcis_token_subject": username}
            )

        return access_token, int(body.get("expires_in") or 60)

    @api.model
    def _token_error(self, response):
        """Phrase a token failure as something an administrator can act on."""
        detail = {}
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = body
        except ValueError:
            pass
        code = detail.get("error") or ""
        description = detail.get("error_description") or ""

        if code == "invalid_grant":
            # A token is bound to the issuer URL it was minted under. Where two
            # hostnames serve the same realm — an ingress alias and the canonical
            # name — a token issued via one and refreshed via the other fails
            # here, and reporting it as "revoked" sends the reader looking in
            # entirely the wrong place. Keycloak names the expected issuer, so
            # pass that through.
            if "issuer" in description.lower():
                return OpenepcisError(
                    _(
                        "The offline token was issued by a different URL than the "
                        "one configured here: %(detail)s\n\nA token is bound to the "
                        "issuer it was minted under. Use the same realm URL that "
                        "issued it — an alias hostname for the same realm counts "
                        "as different.",
                        detail=description,
                    ),
                    status=response.status_code,
                )
            return OpenepcisError(
                _(
                    "The offline token is no longer accepted (%s). It has been "
                    "revoked, or the realm's offline session has been removed. "
                    "Deposit a fresh one.",
                    description or code,
                ),
                status=response.status_code,
            )
        if code == "unauthorized_client":
            return OpenepcisError(
                _(
                    "Keycloak refused the client. Check the client ID, and the "
                    "secret if the client is confidential."
                ),
                status=response.status_code,
            )
        return OpenepcisError(
            _("Keycloak refused to issue a token: %s", description or code or response.text[:200]),
            status=response.status_code,
        )

    @api.model
    def _token_claims(self, token):
        """The claims of a JWT, read without verifying it.

        Only ever used to describe a token back to the administrator who
        deposited it. Nothing is authorised on this basis — the resolver and
        Keycloak do the verifying.
        """
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload))
        except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
            return {}

    @api.model
    def _token_type(self, token):
        return self._token_claims(token).get("typ") or ""

    @api.model
    def _token_subject(self, token):
        claims = self._token_claims(token)
        return claims.get("preferred_username") or claims.get("sub") or ""

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
    def request(self, method, path, payload=None, params=None, company=None, timeout=None):
        """Call the resolver and return the decoded body.

        :returns: the parsed JSON, or ``None`` for an empty body (``204``).
        :raises OpenepcisError: for every non-2xx answer and every transport
            failure. Callers decide whether that aborts them or gets recorded.
        """
        company = self._company(company)
        settings = self._settings(company)
        url = "%s%s" % (settings["base_url"], path if path.startswith("/") else "/" + path)
        method = method.upper()

        tries = MAX_ATTEMPTS if method in IDEMPOTENT_METHODS else 1
        reauth_allowed = True
        last_error = None
        used = 0
        backoff = 0

        while True:
            if backoff:
                time.sleep(backoff)
                backoff = 0

            headers = {
                "Accept": "application/json",
                "Authorization": "Bearer %s" % self._access_token(company),
            }
            if payload is not None:
                headers["Content-Type"] = "application/json"

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
                # idempotent methods get another go, so repeating is safe.
                last_error = OpenepcisError(
                    _(
                        "Could not reach the OpenEPCIS resolver at %(url)s: %(why)s",
                        url=url,
                        why=exc,
                    ),
                    path=path,
                )
                _logger.warning("OpenEPCIS %s %s failed: %s", method, path, exc)
                used += 1
                if used >= tries:
                    raise last_error from exc
                backoff = BACKOFF_SECONDS[min(used - 1, len(BACKOFF_SECONDS) - 1)]
                continue

            # A 401 means the access token lapsed early or was revoked in flight.
            # Minting a new one and going again is *not* a retry of the operation:
            # the call was refused, so nothing was applied. It therefore costs no
            # attempt and needs no backoff — which is what makes it safe even for
            # POST, where repeating the operation itself would not be.
            if response.status_code == 401 and reauth_allowed:
                reauth_allowed = False
                _ACCESS_TOKENS.pop(self._cache_key(company), None)
                last_error = self._error_from(response, path)
                continue

            if response.status_code < 300:
                return self._decode(response)

            error = self._error_from(response, path)
            used += 1
            if error.is_retryable and used < tries:
                last_error = error
                _logger.info(
                    "OpenEPCIS %s %s answered %s, retrying", method, path, response.status_code
                )
                backoff = BACKOFF_SECONDS[min(used - 1, len(BACKOFF_SECONDS) - 1)]
                continue
            raise error

    @api.model
    def post_file(self, path, filename, content, form=None, company=None, timeout=None):
        """Upload a file as multipart form data.

        Separate from :meth:`request` because the body is not JSON and must not
        carry a ``Content-Type`` of its own — ``requests`` sets the multipart
        boundary, and overriding it produces an unparseable request.
        """
        company = self._company(company)
        settings = self._settings(company)
        url = "%s%s" % (settings["base_url"], path if path.startswith("/") else "/" + path)
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer %s" % self._access_token(company),
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
                _("Could not reach the OpenEPCIS resolver at %(url)s: %(why)s", url=url, why=exc),
                path=path,
            ) from exc

        if response.status_code >= 300:
            raise self._error_from(response, path)
        return self._decode(response)

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
        failure here has its own fix — a realm URL, a client setting, a revoked
        token, a missing claim. Returns a list of ``(name, ok, detail)``, where
        ``ok`` may be ``None`` for "this deployment does not offer that".
        """
        company = self._company(company)
        checks = []

        # First: can we get a token at all? Everything else is downstream of it.
        try:
            self._access_token(company, force=True)
        except (OpenepcisError, UserError) as exc:
            checks.append((_("Access token from the offline token"), False, str(exc)))
            return checks
        checks.append((_("Access token from the offline token"), True, ""))

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

        if not probe(_("Resolver accepts the token"), "/products", {"page": 0, "pageSize": 1}):
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
                "%(detail)s — add the claim to the token's user in Keycloak, "
                "then deposit a fresh offline token.",
                detail=error.message,
            )
        if error.status == 401:
            return _("The resolver rejected the access token.")
        if error.status == 403:
            return _(
                "Authenticated, but the identity lacks the tenant role needed to write. "
                "Grant the realm role named after the tenant."
            )
        if optional and error.status in (400, 404, 409):
            return _("Not configured for this tenant — only needed to draw identifiers.")
        return str(error)
