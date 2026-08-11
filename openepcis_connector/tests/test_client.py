# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The client adapter: configuration, token persistence, phrasing, diagnosis.

Transport and retry mechanics live in the vendored ``benelog_client`` and are
tested in that repository. What belongs to *this* addon — and is defended
here — is the seam: settings come off ``res.company`` with a translated
dialog when incomplete, a rotated offline token lands back on the company
record, the token subject is corrected from the first minted access token,
and the library's structured errors surface as translated ``OpenepcisError``
messages an administrator can act on.

The token tests stub ``requests.Session`` underneath the vendored library, so
the whole stack from the Odoo model down to the wire format runs for real.
"""

import base64
import json
from unittest.mock import patch

import requests

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from ..models import openepcis_client as client_module
from ..models.openepcis_client import OpenepcisClient
from ..utils.exceptions import OpenepcisError
from ..vendor.benelog_client.core import auth as lib_auth
from ..vendor.benelog_client.core import client as lib_client
from .common import OpenepcisCase

ISSUER = "https://auth.example.test/realms/openepcis"
DISCOVERY = {"token_endpoint": ISSUER + "/token"}


class _Answer:
    """The little of a requests.Response that the client actually reads."""

    def __init__(self, status_code, body=b"{}", content_type="application/json"):
        self.status_code = status_code
        self.content = body
        self.text = body.decode()
        self.headers = {"Content-Type": content_type}

    def json(self):
        return json.loads(self.content)


def _json(payload, status=200):
    return _Answer(status, json.dumps(payload).encode())


def _jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "header.%s.signature" % payload


class ClientCase(OpenepcisCase):
    """Shared cache hygiene: never reuse an auth minted by another test."""

    def setUp(self):
        super().setUp()
        for cache in (
            client_module._CLIENTS,
            lib_auth._PROTECTED_RESOURCE,
            lib_auth._OIDC_CONFIG,
        ):
            cache.clear()
            self.addCleanup(cache.clear)

    def _patch(self, target, name, value):
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)


@tagged("post_install", "-at_install")
class TestConfiguration(ClientCase):
    def test_missing_settings_name_what_is_missing(self):
        self.company.openepcis_offline_token = False
        with self.assertRaises(UserError) as caught:
            self.env["openepcis.client"]._settings(company=self.company)
        self.assertIn("offline token", str(caught.exception))

    def test_is_configured_is_false_while_switched_off(self):
        self.company.openepcis_enabled = False
        self.assertFalse(self.env["openepcis.client"].is_configured(company=self.company))

    def test_is_configured_needs_a_token(self):
        self.company.openepcis_offline_token = False
        self.assertFalse(self.env["openepcis.client"].is_configured(company=self.company))

    def test_base_url_loses_a_trailing_slash(self):
        self.company.openepcis_base_url = "https://id.example.test/"
        self.assertEqual(
            self.env["openepcis.client"].base_url(company=self.company),
            "https://id.example.test",
        )

    def test_a_resolver_url_with_a_path_is_refused(self):
        with self.assertRaises(ValidationError):
            self.company.openepcis_base_url = "https://id.example.test/products"

    def test_a_realm_url_without_the_realm_is_refused(self):
        # Pointing at the Keycloak host instead of the realm is the usual slip,
        # and discovery then 404s with nothing to explain it.
        with self.assertRaises(ValidationError):
            self.company.openepcis_oidc_issuer = "https://auth.example.test"

    def test_a_discovery_url_is_refused(self):
        with self.assertRaises(ValidationError):
            self.company.openepcis_oidc_issuer = (
                "https://auth.example.test/realms/x/.well-known/openid-configuration"
            )

    def test_depositing_a_token_records_who_it_belongs_to(self):
        self.company.openepcis_offline_token = _jwt(
            {"preferred_username": "svc-odoo", "typ": "Offline"}
        )
        self.assertEqual(self.company.openepcis_token_subject, "svc-odoo")

    def test_an_ordinary_refresh_token_is_refused_on_the_spot(self):
        # It would work for a few minutes and then stop, long after whoever
        # pasted it has moved on.
        with self.assertRaises(ValidationError) as caught:
            self.company.openepcis_offline_token = _jwt(
                {"preferred_username": "someone", "typ": "Refresh"}
            )
        self.assertIn("offline_access", str(caught.exception))

    def test_a_token_that_cannot_be_read_is_still_accepted(self):
        # Keycloak can be configured to issue opaque tokens; refusing one this
        # module simply cannot decode would be presumptuous.
        self.company.openepcis_offline_token = "opaque-token-value"
        self.assertEqual(self.company.openepcis_offline_token, "opaque-token-value")


@tagged("post_install", "-at_install")
class TestErrorClassification(ClientCase):
    def test_a_gateway_timeout_is_worth_retrying(self):
        self.assertTrue(OpenepcisError("timeout", status=504).is_retryable)

    def test_a_validation_failure_is_not(self):
        self.assertFalse(OpenepcisError("bad request", status=400).is_retryable)

    def test_no_answer_at_all_is_worth_retrying(self):
        self.assertTrue(OpenepcisError("unreachable").is_retryable)

    def test_a_missing_claim_is_told_apart_from_a_bad_body(self):
        claim = OpenepcisError(
            "The authenticated identity carries no defaultGroup claim", status=400
        )
        self.assertTrue(claim.is_missing_claim)
        self.assertFalse(OpenepcisError("gtin is required", status=400).is_missing_claim)


@tagged("post_install", "-at_install")
class TestTokens(ClientCase):
    """The real token path, through the vendored library — not stubbed."""

    def setUp(self):
        super().setUp()
        # Discovery pre-seeded: this suite is about the token exchange and
        # its writebacks, not about reading a well-known document.
        lib_auth._OIDC_CONFIG[ISSUER] = DISCOVERY

    def _transport(self, token_answers, api_answers=None):
        """Answer the token endpoint and the resolver through separate stubs."""
        calls = {"token": [], "api": []}
        tokens = list(token_answers)
        apis = list(api_answers or [_json({})])

        def fake_post(_session, url, **kw):
            calls["token"].append(kw.get("data"))
            return tokens[min(len(calls["token"]) - 1, len(tokens) - 1)]

        def fake_request(_session, method, url, **kw):
            calls["api"].append(kw.get("headers", {}).get("Authorization"))
            return apis[min(len(calls["api"]) - 1, len(apis) - 1)]

        self._patch(requests.Session, "post", fake_post)
        self._patch(requests.Session, "request", fake_request)
        self._patch(lib_client, "BACKOFF_SECONDS", (0, 0))
        return calls

    def test_the_access_token_is_minted_from_the_offline_token(self):
        calls = self._transport([_json({"access_token": "fresh", "expires_in": 300})])
        self.env["openepcis.client"].get("/products")

        self.assertEqual(calls["token"][0]["grant_type"], "refresh_token")
        self.assertEqual(calls["token"][0]["refresh_token"], "offline.token.value")
        self.assertEqual(calls["api"][0], "Bearer fresh")

    def test_the_access_token_is_reused_while_it_lasts(self):
        calls = self._transport([_json({"access_token": "fresh", "expires_in": 300})])
        self.env["openepcis.client"].get("/products")
        self.env["openepcis.client"].get("/products")
        self.assertEqual(len(calls["token"]), 1, "one mint, two calls")

    def test_a_rotated_refresh_token_is_stored_on_the_company(self):
        # Keycloak rotates when "Revoke Refresh Token" is on. Losing the new one
        # locks the connector out at the next refresh. The library hands the
        # replacement to OdooTokenStore, and it must land on the record.
        self._transport(
            [_json({"access_token": "a", "expires_in": 300, "refresh_token": "rotated.value"})]
        )
        self.env["openepcis.client"].get("/products")
        self.assertEqual(self.company.openepcis_offline_token, "rotated.value")

    def test_a_revoked_offline_token_says_so(self):
        self._transport(
            [_json({"error": "invalid_grant", "error_description": "Token is not active"}, 400)]
        )
        with self.assertRaises(OpenepcisError) as caught:
            self.env["openepcis.client"].get("/products")
        self.assertIn("no longer accepted", str(caught.exception))

    def test_a_rejected_client_says_so(self):
        self._transport([_json({"error": "unauthorized_client"}, 401)])
        with self.assertRaises(OpenepcisError) as caught:
            self.env["openepcis.client"].get("/products")
        self.assertIn("client ID", str(caught.exception))

    def test_an_issuer_mismatch_is_not_reported_as_revoked(self):
        # Two hostnames can serve one realm — an ingress alias and the canonical
        # name. A token minted via one and refreshed via the other fails with
        # invalid_grant, and calling that "revoked" sends the reader hunting in
        # Keycloak for a session that is perfectly intact.
        self._transport(
            [
                _json(
                    {
                        "error": "invalid_grant",
                        "error_description": "Invalid token issuer. Expected '%s'" % ISSUER,
                    },
                    400,
                )
            ]
        )
        with self.assertRaises(OpenepcisError) as caught:
            self.env["openepcis.client"].get("/products")
        message = str(caught.exception)
        self.assertIn("issued by a different URL", message)
        self.assertNotIn("revoked", message)

    def test_a_401_is_reauthorised_once(self):
        calls = self._transport(
            [
                _json({"access_token": "stale", "expires_in": 300}),
                _json({"access_token": "fresh", "expires_in": 300}),
            ],
            [_Answer(401), _json({})],
        )
        self.env["openepcis.client"].get("/products")

        self.assertEqual(len(calls["token"]), 2, "a second token was minted")
        self.assertEqual(calls["api"], ["Bearer stale", "Bearer fresh"])

    def test_a_post_gets_its_reauthorised_attempt_too(self):
        # Re-authorising is not repeating the operation: the 401 means nothing
        # was applied. A POST that would never be retried must still get this.
        calls = self._transport(
            [
                _json({"access_token": "stale", "expires_in": 300}),
                _json({"access_token": "fresh", "expires_in": 300}),
            ],
            [_Answer(401), _json({"key": "9520000000028"})],
        )
        result = self.env["openepcis.client"].post("/gs1de/keys/draw", {"ai": "01"})

        self.assertEqual(result, {"key": "9520000000028"})
        self.assertEqual(len(calls["api"]), 2)

    def test_the_token_label_is_corrected_from_the_access_token(self):
        # An offline refresh token carries no preferred_username, so a pasted
        # token can only be labelled with its subject UUID until the first mint.
        self.company.openepcis_token_subject = "0c842b8e-uuid-looking"
        access = _jwt({"preferred_username": "svc-odoo"})
        self._transport([_json({"access_token": access, "expires_in": 300})])
        self.env["openepcis.client"].get("/products")
        self.assertEqual(self.company.openepcis_token_subject, "svc-odoo")

    def test_a_changed_setting_builds_a_fresh_client(self):
        # The client cache is keyed by the settings; editing the client ID must
        # not keep exchanging under the old one.
        self._transport([_json({"access_token": "fresh", "expires_in": 300})])
        self.env["openepcis.client"].get("/products")
        before = len(client_module._CLIENTS)
        self.company.openepcis_client_id = "renamed-connector"
        self.env["openepcis.client"].get("/products")
        self.assertEqual(len(client_module._CLIENTS), before + 1)


@tagged("post_install", "-at_install")
class TestRetry(ClientCase):
    """The retry policy is the library's; asserted once through the adapter."""

    def _transport(self, statuses):
        self.stub_access_token()
        calls = []
        answers = list(statuses)

        def fake(_session, method, url, **kw):
            calls.append(method)
            return _Answer(answers[min(len(calls) - 1, len(answers) - 1)])

        self._patch(requests.Session, "request", fake)
        self._patch(lib_client, "BACKOFF_SECONDS", (0, 0))
        return calls

    def test_a_put_is_retried_after_a_gateway_error(self):
        calls = self._transport([503, 200])
        self.env["openepcis.client"].put("/products/9520000000004", {})
        self.assertEqual(len(calls), 2, "PUT is create-or-update, so repeating it is safe")

    def test_a_post_is_never_retried(self):
        # Repeating a POST could answer 409 the second time — or, on the key
        # pool, burn a GS1 number that cannot be handed back.
        calls = self._transport([503, 200])
        with self.assertRaises(OpenepcisError):
            self.env["openepcis.client"].post("/gs1de/keys/draw", {"ai": "01"})
        self.assertEqual(len(calls), 1)

    def test_an_html_answer_is_explained_rather_than_called_bad_json(self):
        self.stub_access_token()
        self._patch(
            requests.Session,
            "request",
            lambda _session, method, url, **kw: _Answer(200, b"<html>login</html>", "text/html"),
        )
        with self.assertRaises(OpenepcisError) as caught:
            self.env["openepcis.client"].get("/products")
        self.assertIn("web page", str(caught.exception))


@tagged("post_install", "-at_install")
class TestDiagnosis(ClientCase):
    def _diagnose(self, answers):
        """Run the diagnosis with a canned answer per path prefix."""

        def fake_request(_self, method, path, payload=None, params=None, **kw):
            for prefix, outcome in answers.items():
                if path.startswith(prefix):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
            return {}

        self.stub_access_token()
        self._patch(OpenepcisClient, "request", fake_request)
        return self.env["openepcis.client"].diagnose(company=self.company)

    def test_the_token_is_checked_before_anything_downstream(self):
        checks = self._diagnose({})
        self.assertIn("token", checks[0][0].lower())
        self.assertTrue(all(ok for _n, ok, _d in checks))

    def test_a_rejected_token_stops_the_diagnosis(self):
        self._patch(
            OpenepcisClient,
            "_access_token",
            lambda _self, company, force=False: (_ for _ in ()).throw(
                OpenepcisError("The offline token is no longer accepted")
            ),
        )
        checks = self.env["openepcis.client"].diagnose(company=self.company)
        self.assertEqual(len(checks), 1, "nothing downstream is worth probing")
        self.assertFalse(checks[0][1])

    def test_a_missing_claim_names_the_fix(self):
        checks = self._diagnose(
            {
                "/sync/channels": OpenepcisError(
                    "The authenticated identity carries no defaultGroup claim", status=400
                )
            }
        )
        claim_check = next(c for c in checks if "defaultGroup" in c[0])
        self.assertFalse(claim_check[1])
        self.assertIn("Keycloak", claim_check[2])

    def test_an_older_deployment_is_reported_as_absent_not_broken(self):
        # The demo deployment predates /sync/channels. Calling that a missing
        # claim would send somebody hunting in Keycloak for nothing.
        checks = self._diagnose({"/sync/channels": OpenepcisError("not found", status=404)})
        claim_check = next(c for c in checks if "defaultGroup" in c[0])
        self.assertIsNone(claim_check[1], "neither pass nor fail")
        self.assertIn("predates", claim_check[2])

    def test_no_gs1_licence_is_not_a_failure(self):
        checks = self._diagnose({"/gs1de/keys": OpenepcisError("no client", status=400)})
        prefix_check = next(c for c in checks if "prefix" in c[0])
        self.assertIn("only needed", prefix_check[2])


@tagged("post_install", "-at_install")
class TestDiscovery(ClientCase):
    """The realm is discovered from the resolver (RFC 9728), not pasted in.

    The discovery itself is the library's; what the adapter owes is the
    wiring — no realm configured means discovery runs, a configured realm
    means it never does.
    """

    def test_the_realm_is_discovered_from_the_resolver(self):
        self.company.openepcis_oidc_issuer = False
        seen = {}

        def fake_get(_session, url, **kw):
            seen["url"] = url
            return _json({"authorization_servers": [ISSUER]})

        self._patch(requests.Session, "get", fake_get)
        auth, _client = self.env["openepcis.client"]._bound(self.company)
        self.assertEqual(auth.issuer(), ISSUER)
        self.assertEqual(
            seen["url"], "https://id.example.test/.well-known/oauth-protected-resource"
        )

    def test_a_configured_realm_overrides_discovery(self):
        called = {"n": 0}

        def fake_get(_session, url, **kw):
            called["n"] += 1
            return _json({})

        self._patch(requests.Session, "get", fake_get)
        auth, _client = self.env["openepcis.client"]._bound(self.company)
        self.assertEqual(auth.issuer(), ISSUER)
        self.assertEqual(called["n"], 0, "a configured realm must not trigger discovery")

    def test_a_resolver_without_metadata_is_explained(self):
        self.company.openepcis_oidc_issuer = False
        self._patch(
            requests.Session,
            "get",
            lambda _session, url, **kw: _Answer(404, b"not found", "text/plain"),
        )
        auth, _client = self.env["openepcis.client"]._bound(self.company)
        with self.assertRaises(Exception) as caught:
            auth.issuer()
        self.assertIn("does not publish OAuth metadata", str(caught.exception))
