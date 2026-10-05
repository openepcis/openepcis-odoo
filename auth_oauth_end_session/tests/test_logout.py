# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
from urllib.parse import parse_qs, urlsplit

from odoo.tests import HttpCase, new_test_user, tagged

# new_test_user sets the password to the login (padded to 8 characters,
# which both logins here already have); tests authenticate with it.
ENDPOINT = "https://auth.example.com/realms/demo/protocol/openid-connect/logout"


@tagged("post_install", "-at_install")
class TestEndSessionLogout(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.provider = cls.env["auth.oauth.provider"].create(
            {
                "name": "Test IdP",
                "client_id": "odoo-test",
                "auth_endpoint": "https://auth.example.com/auth",
                "validation_endpoint": "https://auth.example.com/userinfo",
                "end_session_endpoint": ENDPOINT,
                "body": "Sign in with Test IdP",
            }
        )
        cls.sso_user = new_test_user(cls.env, login="sso-user")
        cls.sso_user.oauth_provider_id = cls.provider
        new_test_user(cls.env, login="local-user")

    def _logout(self):
        response = self.url_open("/web/session/logout", allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        return response.headers["Location"]

    def test_sso_user_is_sent_to_the_provider(self):
        self.authenticate("sso-user", "sso-user")
        location = self._logout()
        parts = urlsplit(location)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", ENDPOINT)
        query = parse_qs(parts.query)
        self.assertEqual(query["client_id"], ["odoo-test"])
        self.assertTrue(query["post_logout_redirect_uri"][0].endswith("/web/login"))

    def test_password_user_stays_in_odoo(self):
        self.authenticate("local-user", "local-user")
        self.assertFalse(self._logout().startswith(ENDPOINT))

    def test_provider_without_endpoint_stays_in_odoo(self):
        self.provider.end_session_endpoint = False
        self.authenticate("sso-user", "sso-user")
        self.assertFalse(self._logout().startswith(ENDPOINT))

    def test_url_keeps_an_existing_query(self):
        self.provider.end_session_endpoint = ENDPOINT + "?ui_locales=de"
        url = self.provider._end_session_url("https://odoo.example.com/")
        self.assertIn("?ui_locales=de&client_id=odoo-test", url)
