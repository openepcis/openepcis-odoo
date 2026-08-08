# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Reading what a downstream registry requires, and reporting what is missing.

This is advisory by design. The catalog accepts an incomplete record — a
passport is filled in over time and by several people — so an unmet requirement
must inform, never block.
"""

import json

from odoo.tests import tagged

from .common import OpenepcisCase

CHANNELS = [
    {
        "id": "gs1de",
        "displayName": "GS1 Germany Service Platform",
        "supportedKinds": ["PRODUCT", "ORGANIZATION"],
        "enabled": True,
        "dryRun": True,
        "configured": True,
        "requiredTerms": {
            "PRODUCT": ["gs1:brand", "gs1:gpcCategoryCode", "gs1:netContent", "gs1:targetMarket"]
        },
    }
]


@tagged("post_install", "-at_install")
class TestChannel(OpenepcisCase):
    def test_refresh_stores_what_the_resolver_lists(self):
        self.capture(result=CHANNELS)
        channels = self.env["openepcis.channel"].refresh(company=self.company)

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels.channel_id, "gs1de")
        self.assertTrue(channels.dry_run)
        self.assertEqual(json.loads(channels.required_terms_json)["PRODUCT"][0], "gs1:brand")

    def test_refresh_drops_a_channel_the_deployment_no_longer_has(self):
        self.capture(result=CHANNELS)
        self.env["openepcis.channel"].refresh(company=self.company)
        self.capture(result=[])
        self.env["openepcis.channel"].refresh(company=self.company)
        self.assertFalse(
            self.env["openepcis.channel"].search([("company_id", "=", self.company.id)])
        )

    def test_required_terms_lose_their_prefix(self):
        self.capture(result=CHANNELS)
        self.env["openepcis.channel"].refresh(company=self.company)
        terms = self.env["openepcis.channel"].required_terms("PRODUCT", company=self.company)
        self.assertEqual(terms, {"brand", "gpcCategoryCode", "netContent", "targetMarket"})

    def test_a_disabled_channel_asks_for_nothing(self):
        self.capture(result=CHANNELS)
        self.env["openepcis.channel"].refresh(company=self.company).enabled = False
        self.assertEqual(
            self.env["openepcis.channel"].required_terms("PRODUCT", company=self.company), set()
        )


@tagged("post_install", "-at_install")
class TestReadiness(OpenepcisCase):
    def setUp(self):
        super().setUp()
        self.capture(result=CHANNELS)
        self.env["openepcis.channel"].refresh(company=self.company)

    def test_a_complete_product_needs_nothing(self):
        self.assertEqual(self._product().openepcis_missing_terms, "")

    def test_an_empty_brand_is_named(self):
        product = self._product(openepcis_brand_name=False)
        self.assertEqual(product.openepcis_missing_terms, "brand")

    def test_several_gaps_are_listed_together(self):
        product = self._product(
            openepcis_brand_name=False,
            openepcis_net_content=0.0,
        )
        self.assertEqual(product.openepcis_missing_terms, "brand, netContent")

    def test_a_gap_behind_a_relation_is_seen(self):
        self.category.openepcis_gpc_code = False
        self.assertIn("gpcCategoryCode", self._product().openepcis_missing_terms)

    def test_an_incomplete_product_is_still_published(self):
        # Requirements inform; they do not hold data hostage.
        calls = self.capture(result=CHANNELS)
        product = self._product(openepcis_brand_name=False)
        product.openepcis_publish = True
        product._openepcis_sync()

        self.assertEqual(product.openepcis_state, "synced")
        self.assertTrue(any(call["method"] == "PUT" for call in calls))
