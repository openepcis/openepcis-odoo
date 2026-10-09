# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
{
    "name": "OAuth: end the provider session on logout",
    "summary": "Logging out of Odoo also logs out of the OpenID provider",
    "description": """
Odoo's OAuth login (auth_oauth) only ends the Odoo session on logout. The
session at the identity provider stays, so the next "Sign in with ..." logs
straight back in as the same user, without asking.

With an end-session endpoint on the provider (OpenID Connect RP-initiated
logout, e.g. Keycloak's .../protocol/openid-connect/logout), logging out of
Odoo sends users who signed in through that provider on to it, and the
provider returns them to the Odoo login page.

Users who log in with a password are not affected.
""",
    "author": "benelog GmbH & Co. KG",
    "website": "https://openepcis.io/docs/connectors/odoo/single-sign-on-logout",
    "category": "Hidden/Tools",
    "version": "18.0.1.0.0",
    "license": "LGPL-3",
    "depends": ["auth_oauth"],
    "data": ["views/auth_oauth_provider_views.xml"],
    "installable": True,
}
