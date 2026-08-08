# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Where the connection lives.

On the company rather than in ``ir.config_parameter``, because a GS1 licence and
its company prefix belong to a legal entity: a multi-company database publishes
to one tenant per company, with different credentials, and a global parameter
could not express that.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = "res.company"

    openepcis_enabled = fields.Boolean(
        string="Publish to OpenEPCIS",
        help="Queue products and partners for publication. Turning this off "
        "stops the queue from being drained; nothing already published is withdrawn.",
    )
    openepcis_base_url = fields.Char(
        string="Resolver URL",
        help="Origin of the GS1 Digital Link resolver, e.g. https://id.epcis.cloud. "
        "Also the prefix of every Digital Link this connector shows.",
    )
    # Restricted to administrators: an ordinary user publishes records without
    # ever needing to see the credential that does it.
    openepcis_api_key = fields.Char(
        string="API key",
        groups="base.group_system",
    )
    openepcis_api_secret = fields.Char(
        string="API secret",
        groups="base.group_system",
    )

    def _check_openepcis_base_url(self, url):
        if url and not url.startswith(("http://", "https://")):
            raise ValidationError(
                _("The resolver URL must start with http:// or https:// — got %s.", url)
            )
        if url and url.rstrip("/").endswith(("/products", "/organizations", "/places")):
            # A frequent first-run mistake: pasting the endpoint instead of the
            # origin, which then yields /products/products and a puzzling 404.
            raise ValidationError(
                _(
                    "The resolver URL is the origin only, without a path — "
                    "use https://id.epcis.cloud, not %s.",
                    url,
                )
            )

    def write(self, vals):
        if "openepcis_base_url" in vals:
            self._check_openepcis_base_url(vals["openepcis_base_url"])
        return super().write(vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("openepcis_base_url"):
                self._check_openepcis_base_url(vals["openepcis_base_url"])
        return super().create(vals_list)
