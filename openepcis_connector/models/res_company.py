# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Where the connection lives.

On the company rather than in ``ir.config_parameter``, because a GS1 licence and
its company prefix belong to a legal entity: a multi-company database publishes
to one tenant per company, with different credentials, and a global parameter
could not express that.

The stored credential is an **OIDC offline token** — a refresh token issued with
the ``offline_access`` scope. Odoo never holds a long-lived access token and
never holds a user's password; it exchanges the offline token for a short-lived
access token when it needs one. Revoking access is done in Keycloak, by removing
the offline session, and takes effect without anyone touching Odoo.
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
    openepcis_oidc_issuer = fields.Char(
        string="Keycloak realm URL (optional)",
        help="Usually leave this empty: the realm is discovered from the resolver "
        "itself (RFC 9728 protected-resource metadata). Set it only for a "
        "resolver that does not publish that metadata, e.g. "
        "https://auth.epcis.cloud/realms/openepcis.",
    )
    openepcis_client_id = fields.Char(
        string="Client ID",
        default="integration-connector",
        help="The Keycloak client this connector authenticates as.",
    )
    # Restricted to administrators: an ordinary user publishes records without
    # ever needing to see the credential that does it.
    openepcis_client_secret = fields.Char(
        string="Client secret",
        groups="base.group_system",
        help="Only for a confidential client. Leave empty for a public one.",
    )
    # Char rather than Text on purpose: Odoo renders a Text field as a textarea,
    # which ignores password="True" and puts the token on screen in clear. Char
    # has no length limit in Postgres, and a JWT fits comfortably.
    openepcis_offline_token = fields.Char(
        string="Offline token",
        groups="base.group_system",
        help="An OIDC refresh token issued with the offline_access scope. Short-lived "
        "access tokens are minted from it as needed.",
    )
    openepcis_token_subject = fields.Char(
        string="Token issued for",
        readonly=True,
        help="The user the offline token belongs to. Its roles and claims are the "
        "ones the resolver sees.",
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

    def _check_openepcis_issuer(self, url):
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            raise ValidationError(
                _("The realm URL must start with http:// or https:// — got %s.", url)
            )
        if "/realms/" not in url:
            # Pointing at the Keycloak host instead of the realm is the usual
            # slip, and discovery then 404s with nothing to explain it.
            raise ValidationError(
                _(
                    "The realm URL includes the realm itself — "
                    "https://auth.example.org/realms/openepcis, not %s.",
                    url,
                )
            )
        if url.rstrip("/").endswith("/.well-known/openid-configuration"):
            raise ValidationError(
                _("Give the realm URL, not its discovery document — drop the /.well-known part.")
            )

    def write(self, vals):
        if "openepcis_base_url" in vals:
            self._check_openepcis_base_url(vals["openepcis_base_url"])
        if "openepcis_oidc_issuer" in vals:
            self._check_openepcis_issuer(vals["openepcis_oidc_issuer"])
        if vals.get("openepcis_offline_token"):
            vals = self._openepcis_describe_token(vals)
        return super().write(vals)

    def _openepcis_describe_token(self, vals):
        """Check a deposited token as far as is possible, and label it.

        A refresh token that is *not* an offline one works for a few minutes and
        then stops, long after whoever pasted it has moved on. Keycloak says
        which it is in the ``typ`` claim, so the mistake is worth catching here.

        A token that cannot be decoded is accepted rather than refused: Keycloak
        can be configured to issue opaque tokens, and refusing one this module
        simply cannot read would be presumptuous. Test connection settles it.
        """
        client = self.env["openepcis.client"]
        token = vals["openepcis_offline_token"]
        token_type = client._token_type(token)
        if token_type and token_type.lower() != "offline":
            raise ValidationError(
                _(
                    "That is a '%(type)s' token, not an offline one. It will stop "
                    "working within minutes.\n\nIssue the token with the "
                    "offline_access scope — in the OpenEPCIS web interface, under "
                    "your profile.",
                    type=token_type,
                )
            )
        if "openepcis_token_subject" not in vals:
            vals = dict(vals, openepcis_token_subject=client._token_subject(token))
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("openepcis_base_url"):
                self._check_openepcis_base_url(vals["openepcis_base_url"])
            if vals.get("openepcis_oidc_issuer"):
                self._check_openepcis_issuer(vals["openepcis_oidc_issuer"])
            if vals.get("openepcis_offline_token"):
                vals.update(self._openepcis_describe_token(vals))
        return super().create(vals_list)
