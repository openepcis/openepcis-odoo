# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The Odoo adapter around the vendored ``benelog_client`` library.

An ``AbstractModel`` rather than a plain module so that it can be overridden by
another addon and stubbed in tests without patching imports:

    self.env["openepcis.client"].get("/sync/channels")

Transport, authentication and retry semantics live in the library
(``vendor/benelog_client``); this model contributes exactly the parts only Odoo
can know:

- **Configuration** from ``res.company`` fields, with a translated dialog when
  something is missing.
- **Persistence** of the offline token via :class:`OdooTokenStore` — including
  the rotated replacement Keycloak may answer with on every exchange, which
  must land back on the company record or the connector locks itself out.
- **Phrasing**: the library raises structured, English
  :class:`~..vendor.benelog_client.core.errors.BenelogError`; this adapter
  re-phrases the cases an administrator acts on with ``_()`` and re-raises
  everything as the addon's own :class:`~..utils.exceptions.OpenepcisError`,
  so no caller changes.

**Authentication is an OIDC offline token.** What Odoo stores is a refresh
token issued with the ``offline_access`` scope; every call carries a
short-lived access token minted from it. This module only ever consumes such a
token; issuing one belongs where a human is present in a browser — the
platform's own web interface. So the token is deposited by an administrator,
and the library exchanges it.

The practical consequence for whoever sets this up is that the claims the
resolver insists on (``defaultGroup``, ``gs1CompanyPrefix``, the tenant role)
come from the user the token was issued for. There is no service account to
provision separately.

Only ``requests`` is used (by the library), which Odoo already depends on — the
connector adds no package of its own, which is what keeps it installable on
Odoo Online.
"""

import logging

from odoo import _, api, models
from odoo.exceptions import UserError

from ..utils.exceptions import OpenepcisError
from ..vendor.benelog_client.core.auth import OfflineTokenAuth, token_subject, token_type
from ..vendor.benelog_client.core.client import Client
from ..vendor.benelog_client.core.config import ClientConfig
from ..vendor.benelog_client.core.errors import BenelogError

_logger = logging.getLogger(__name__)

#: One (store, auth, client) triple per configuration, keyed by
#: (database, company, settings). Per worker and deliberately not in the
#: database: the auth object caches an access token that lives minutes, and
#: writing one on every refresh would put a short-lived secret into the table
#: and the audit log for no gain. A restart costs one extra token request.
#: Changing any setting changes the key, so a stale client is never reused.
_CLIENTS = {}


class OdooTokenStore:
    """The library's ``TokenStore`` protocol, backed by ``res.company``.

    Bound to the *current* recordset before every call — a recordset holds a
    cursor, and a cursor must never outlive its request. The writes use
    ``openepcis_syncing`` so the company's own write hooks know the change
    comes from the connector, not from an administrator.
    """

    def __init__(self):
        self._company = None

    def bind(self, company):
        # sudo: an ordinary user may publish a product without being allowed
        # to read the offline token, which is restricted to administrators.
        self._company = company.sudo()

    def get_offline_token(self):
        return self._company.openepcis_offline_token or ""

    def save_offline_token(self, token):
        self._company.with_context(openepcis_syncing=True).write({"openepcis_offline_token": token})
        _logger.info("OpenEPCIS: offline token rotated by the identity provider")

    def save_subject(self, username):
        if self._company.openepcis_token_subject != username:
            self._company.with_context(openepcis_syncing=True).write(
                {"openepcis_token_subject": username}
            )


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
        """Connection settings, or a message naming what is missing.

        The realm URL is not required: the library discovers it from the
        resolver (RFC 9728). ``openepcis_oidc_issuer`` stays as an optional
        override for a deployment that publishes no metadata.
        """
        company = self._company(company).sudo()
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
        return {
            "base_url": company.openepcis_base_url.rstrip("/"),
            "issuer_override": (company.openepcis_oidc_issuer or "").rstrip("/"),
            "client_id": company.openepcis_client_id,
            "client_secret": company.openepcis_client_secret or "",
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
    # The library client
    # ------------------------------------------------------------------

    @api.model
    def _bound(self, company):
        """The (auth, client) pair for a company, its store bound to *this* env."""
        settings = self._settings(company)
        key = (self.env.cr.dbname, company.id, tuple(sorted(settings.items())))
        entry = _CLIENTS.get(key)
        if entry is None:
            store = OdooTokenStore()
            auth = OfflineTokenAuth(
                ClientConfig(base_url=settings["base_url"]),
                store,
                client_id=settings["client_id"],
                client_secret=settings["client_secret"],
                issuer_override=settings["issuer_override"],
            )
            entry = (store, auth, Client(ClientConfig(base_url=settings["base_url"]), auth))
            _CLIENTS[key] = entry
        store, auth, client = entry
        store.bind(company)
        return auth, client

    @api.model
    def _access_token(self, company, force=False):
        """A usable access token, minted from the offline token when needed."""
        auth, _client = self._bound(self._company(company))
        if force:
            auth.invalidate()
        try:
            return auth.bearer()
        except BenelogError as exc:
            raise self._adapt(exc) from exc

    @api.model
    def _token_type(self, token):
        return token_type(token)

    @api.model
    def _token_subject(self, token):
        return token_subject(token)

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
        _auth, client = self._bound(self._company(company))
        try:
            return client.request(method, path, payload=payload, params=params, timeout=timeout)
        except BenelogError as exc:
            raise self._adapt(exc) from exc

    @api.model
    def post_file(self, path, filename, content, form=None, company=None, timeout=None):
        """Upload a file as multipart form data."""
        _auth, client = self._bound(self._company(company))
        try:
            return client.post_file(path, filename, content, form=form, timeout=timeout)
        except BenelogError as exc:
            raise self._adapt(exc) from exc

    # ------------------------------------------------------------------
    # Phrasing
    # ------------------------------------------------------------------

    @api.model
    def _adapt(self, error):
        """The library's structured error, re-phrased where a translation helps.

        The token failures are the ones an administrator has to act on, so
        those get ``_()`` sentences keyed on the structured facts (the OAuth
        error code, never the English prose). Everything else passes through:
        resolver errors already carry the server's own RFC 7807 ``detail``.
        """
        problem = error.problem or {}
        code = str(problem.get("error") or "")
        description = str(problem.get("error_description") or "")

        if code == "invalid_grant":
            # A token is bound to the issuer URL it was minted under. Where
            # two hostnames serve the same realm, a token issued via one and
            # refreshed via the other fails here, and reporting it as
            # "revoked" sends the reader looking in entirely the wrong place.
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
                    status=error.status,
                    problem=problem,
                )
            return OpenepcisError(
                _(
                    "The offline token is no longer accepted (%s). It has been "
                    "revoked, or the realm's offline session has been removed. "
                    "Deposit a fresh one.",
                    description or code,
                ),
                status=error.status,
                problem=problem,
            )
        if code == "unauthorized_client":
            return OpenepcisError(
                _(
                    "Keycloak refused the client. Check the client ID, and the "
                    "secret if the client is confidential."
                ),
                status=error.status,
                problem=problem,
            )
        return OpenepcisError(error.message, status=error.status, problem=problem, path=error.path)

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
