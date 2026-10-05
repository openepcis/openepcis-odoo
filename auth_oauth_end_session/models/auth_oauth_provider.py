# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
from urllib.parse import urlencode

from odoo import fields, models


class AuthOAuthProvider(models.Model):
    _inherit = "auth.oauth.provider"

    end_session_endpoint = fields.Char(
        string="End session URL",
        help="OpenID Connect end_session_endpoint, e.g. "
        "https://auth.example.com/realms/<realm>/protocol/openid-connect/logout. "
        "When set, logging out of Odoo also ends the session at this provider.",
    )

    def _end_session_url(self, base_url):
        """Where to send a user of this provider after the Odoo logout.

        auth_oauth uses the implicit flow and never receives an ID token, so
        there is no id_token_hint to pass. client_id is the documented
        alternative; the provider may then ask the user to confirm the logout.
        The post-logout redirect must be registered with the provider (in
        Keycloak: "Valid post logout redirect URIs" of the client).
        """
        self.ensure_one()
        if not self.end_session_endpoint:
            return None
        query = urlencode(
            {
                "client_id": self.client_id,
                "post_logout_redirect_uri": base_url.rstrip("/") + "/web/login",
            }
        )
        separator = "&" if "?" in self.end_session_endpoint else "?"
        return self.end_session_endpoint + separator + query
