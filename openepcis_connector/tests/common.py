# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Shared scaffolding: a configured company and a resolver that never exists.

Every test here runs against a stubbed HTTP client. Talking to a real resolver
from a unit test would make the suite depend on a network, on credentials, and —
because a confirmed key is registered with GS1 for good — on nothing ever going
wrong. The live check belongs in the documented smoke test, not here.

The stubs patch :meth:`OpenepcisClient.request`, which sits *above* token
handling, so most tests never touch Keycloak either. The tests that do care about
tokens stub the transport instead; see ``test_client``.
"""

from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.openepcis_client import OpenepcisClient
from ..utils.exceptions import OpenepcisError

CLIENT_REQUEST = "odoo.addons.openepcis_connector.models.openepcis_client.OpenepcisClient.request"

# GS1 reserves the 952 prefix for testing, so nothing constructed here can ever
# collide with a real company's identifiers.
TEST_GTIN = "9520000000004"
TEST_GTIN_2 = "9520000000011"
TEST_GLN = "9520000000004"


class OpenepcisCase(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.write(
            {
                "openepcis_enabled": True,
                "openepcis_base_url": "https://id.example.test",
                "openepcis_oidc_issuer": "https://auth.example.test/realms/openepcis",
                "openepcis_client_id": "odoo-connector",
                "openepcis_offline_token": "offline.token.value",
            }
        )
        cls.category = cls.env["product.category"].create(
            {"name": "Test category", "openepcis_gpc_code": "10000045"}
        )

    def stub_access_token(self, value="stub-access-token"):
        """Skip token minting for tests that are about something else.

        Called explicitly rather than in ``setUp``: the tests that exercise token
        handling itself must not have it stubbed out from under them, and a stub
        installed for everyone would have hidden exactly the bugs those tests
        exist to catch.
        """
        patcher = patch.object(
            OpenepcisClient,
            "_access_token",
            lambda _self, company, force=False: value,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _product(self, **values):
        """A product complete enough to publish, unless a test says otherwise."""
        germany = self.env.ref("base.de")
        defaults = {
            "name": "Test product",
            "barcode": TEST_GTIN,
            "categ_id": self.category.id,
            "openepcis_brand_name": "Test brand",
            "openepcis_net_content": 500.0,
            "openepcis_net_content_uom_id": self.env.ref("uom.product_uom_unit").id,
            "openepcis_country_of_origin_id": germany.id,
            "openepcis_target_market_ids": [(6, 0, [germany.id])],
        }
        defaults.update(values)
        template = self.env["product.template"].create(defaults)
        return template.product_variant_ids[:1]

    def capture(self, result=None, error=None):
        """Patch the client and collect the calls it would have made.

        Returns a list that fills up as calls happen, so a test can assert on
        method, path and payload without a live server.
        """
        calls = []

        def fake_request(_self, method, path, payload=None, params=None, **kw):
            calls.append({"method": method, "path": path, "payload": payload, "params": params})
            if error is not None:
                raise error
            return result if result is not None else {}

        patcher = patch.object(OpenepcisClient, "request", fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


__all__ = [
    "CLIENT_REQUEST",
    "TEST_GLN",
    "TEST_GTIN",
    "TEST_GTIN_2",
    "OpenepcisCase",
    "OpenepcisError",
]
