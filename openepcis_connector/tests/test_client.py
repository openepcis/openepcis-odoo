# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The HTTP client: configuration, error mapping and the connection diagnosis.

The diagnosis is worth testing carefully, because it is the first thing anyone
touches and the only place that turns a resolver's terse status code into an
instruction. Getting it wrong sends people into Keycloak to hunt for a claim
that was never the problem.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.openepcis_client import OpenepcisClient
from ..utils.exceptions import OpenepcisError
from .common import OpenepcisCase


@tagged("post_install", "-at_install")
class TestConfiguration(OpenepcisCase):
    def test_missing_settings_name_what_is_missing(self):
        self.company.openepcis_api_secret = False
        with self.assertRaises(UserError) as caught:
            self.env["openepcis.client"]._settings(company=self.company)
        self.assertIn("API secret", str(caught.exception))

    def test_is_configured_is_false_while_switched_off(self):
        self.company.openepcis_enabled = False
        self.assertFalse(self.env["openepcis.client"].is_configured(company=self.company))

    def test_base_url_loses_a_trailing_slash(self):
        self.company.openepcis_base_url = "https://id.example.test/"
        self.assertEqual(
            self.env["openepcis.client"].base_url(company=self.company),
            "https://id.example.test",
        )

    def test_a_url_with_a_path_is_refused_at_entry(self):
        from odoo.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self.company.openepcis_base_url = "https://id.example.test/products"

    def test_a_url_without_a_scheme_is_refused(self):
        from odoo.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self.company.openepcis_base_url = "id.example.test"


@tagged("post_install", "-at_install")
class TestErrorClassification(OpenepcisCase):
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
class TestDiagnosis(OpenepcisCase):
    def _diagnose(self, answers):
        """Run the diagnosis with a canned answer per path prefix."""

        def fake_request(_self, method, path, payload=None, params=None, **kw):
            for prefix, outcome in answers.items():
                if path.startswith(prefix):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
            return {}

        patcher = patch.object(OpenepcisClient, "request", fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.env["openepcis.client"].diagnose(company=self.company)

    def test_everything_working_reports_every_step(self):
        checks = self._diagnose({})
        self.assertTrue(all(ok for _n, ok, _d in checks))
        self.assertEqual(len(checks), 3)

    def test_rejected_credentials_stop_the_diagnosis(self):
        checks = self._diagnose({"/products": OpenepcisError("nope", status=401)})
        self.assertEqual(len(checks), 1, "no point probing further")
        self.assertFalse(checks[0][1])
        self.assertIn("rejected", checks[0][2])

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


class _Answer:
    """The little of a requests.Response that the client actually reads."""

    def __init__(self, status_code, body=b"{}", content_type="application/json"):
        self.status_code = status_code
        self.content = body
        self.text = body.decode()
        self.headers = {"Content-Type": content_type}

    def json(self):
        import json

        return json.loads(self.content)


@tagged("post_install", "-at_install")
class TestRetry(OpenepcisCase):
    """Retrying is only safe for verbs the catalog treats as repeatable."""

    def _transport(self, statuses):
        """Patch the transport itself so the retry loop above it really runs."""
        calls = []
        answers = list(statuses)

        def fake(method, url, **kw):
            calls.append(method)
            return _Answer(answers[min(len(calls) - 1, len(answers) - 1)])

        module = "odoo.addons.openepcis_connector.models.openepcis_client"
        for target, value in (("requests.request", fake), ("BACKOFF_SECONDS", (0, 0))):
            patcher = patch("%s.%s" % (module, target), value)
            patcher.start()
            self.addCleanup(patcher.stop)
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

    def test_retrying_eventually_gives_up(self):
        calls = self._transport([503])
        with self.assertRaises(OpenepcisError):
            self.env["openepcis.client"].get("/products")
        self.assertEqual(len(calls), 3, "three attempts, then the error surfaces")

    def test_a_validation_failure_is_not_retried(self):
        calls = self._transport([400])
        with self.assertRaises(OpenepcisError):
            self.env["openepcis.client"].put("/products/9520000000004", {})
        self.assertEqual(len(calls), 1, "a bad body will be just as bad next time")

    def test_an_html_answer_is_explained_rather_than_called_bad_json(self):
        module = "odoo.addons.openepcis_connector.models.openepcis_client"
        patcher = patch(
            "%s.requests.request" % module,
            lambda method, url, **kw: _Answer(200, b"<html>login</html>", "text/html"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.assertRaises(OpenepcisError) as caught:
            self.env["openepcis.client"].get("/products")
        self.assertIn("web page", str(caught.exception))
