# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""A second client, for the second service.

Same credential, different address. The offline token this connector already
holds is the one that authenticates capture too — there is no separate key to
deposit, and a deployment that asks for one has misread this.

What does differ is the permission behind it. Publishing master data and
capturing events are two rights, and a token can hold the first without the
second; the repository then answers 403 without saying which role it missed.
:meth:`_epcis_check` turns that into a sentence, at setup time.
"""

from odoo import _, api, models
from odoo.exceptions import UserError

from ..vendored import BenelogError, Capture, Client, ClientConfig

#: Cached per (database, company, address). The client is a thin wrapper over a
#: requests session; building one per event would open a connection per event.
_CAPTURES = {}


class OpenepcisClient(models.AbstractModel):
    _inherit = "openepcis.client"

    @api.model
    def _epcis_url(self, company=None):
        return (self._company(company).sudo().openepcis_epcis_url or "").rstrip("/")

    @api.model
    def _epcis_configured(self, company=None):
        """Whether an event could be delivered at all, without raising."""
        company = self._company(company).sudo()
        return bool(
            company.openepcis_events_enabled
            and company.openepcis_epcis_url
            and self.is_configured(company)
        )

    @api.model
    def _epcis_capture(self, company=None):
        """The capture service for a company's repository."""
        company = self._company(company)
        url = self._epcis_url(company)
        if not url:
            raise UserError(
                _(
                    "No EPCIS repository is configured for %(company)s.\n"
                    "Settings > General Settings > OpenEPCIS.",
                    company=company.display_name,
                )
            )
        auth, _client = self._bound(company.sudo())
        key = (self.env.cr.dbname, company.id, url)
        capture = _CAPTURES.get(key)
        if capture is None:
            capture = Capture(Client(ClientConfig(base_url=url), auth))
            _CAPTURES[key] = capture
        return capture

    @api.model
    def _epcis_check(self, company=None):
        """What stands between this deployment and a captured event.

        An empty string means nothing does. Anything else is meant to be shown
        to whoever is configuring it, while they can still act on it.
        """
        company = self._company(company)
        if not company.sudo().openepcis_epcis_url:
            return _("No EPCIS repository address is deposited.")
        try:
            return self._epcis_capture(company).check()
        except BenelogError as error:
            return str(error)
