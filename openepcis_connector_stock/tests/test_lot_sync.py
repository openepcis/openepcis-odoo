# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Instance publication: paths, the product gate, and the instance document.

The behaviours pinned here are what make lot publication trustworthy: the path
says exactly what the record is (batch or unit, encoded so no lot number can
change it), nothing is published under a GTIN the catalog does not hold, and a
waiting lot follows its product without anyone touching it again.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import TEST_GTIN_14, LotCase


@tagged("post_install", "-at_install")
class TestQualifierPaths(LotCase):
    def test_a_lot_publishes_to_the_batch_path(self):
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._published_product(tracking="lot"))
        lot.openepcis_publish = True
        lot._openepcis_sync()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "PUT")
        self.assertEqual(calls[0]["path"], "/products/%s/10/BATCH-952-A" % TEST_GTIN_14)
        self.assertEqual(lot.openepcis_state, "synced")

    def test_a_serial_publishes_to_the_serial_path(self):
        calls = self.capture()
        lot = self._lot("952-0007", self._published_product(tracking="serial"))
        lot.openepcis_publish = True
        lot._openepcis_sync()

        self.assertEqual(calls[0]["path"], "/products/%s/21/952-0007" % TEST_GTIN_14)

    def test_an_untracked_product_falls_back_to_the_batch_path(self):
        # A lot on an untracked product exists because a person created one on
        # purpose; a batch is the weaker, safer claim than a unique unit.
        calls = self.capture()
        lot = self._lot("952-ODD", self._published_product(tracking="none"))
        lot.openepcis_publish = True
        lot._openepcis_sync()

        self.assertEqual(calls[0]["path"], "/products/%s/10/952-ODD" % TEST_GTIN_14)

    def test_the_qualifier_value_is_rfc3986_encoded(self):
        # Lot numbers contain whatever people put in lot numbers. A raw slash
        # would silently change the path the resolver sees.
        calls = self.capture()
        lot = self._lot("LOT/2026 A+B", self._published_product())
        lot.openepcis_publish = True
        lot._openepcis_sync()

        self.assertEqual(calls[0]["path"], "/products/%s/10/LOT%%2F2026%%20A%%2BB" % TEST_GTIN_14)
        self.assertEqual(lot.openepcis_state, "synced")

    def test_the_digital_link_extends_to_the_instance(self):
        self.capture()
        lot = self._lot("BATCH-952-A", self._published_product())
        lot.openepcis_publish = True

        self.assertFalse(lot.openepcis_digital_link, "no link before publication")
        lot._openepcis_sync()
        self.assertEqual(
            lot.openepcis_digital_link,
            "https://id.example.test/01/%s/10/BATCH-952-A" % TEST_GTIN_14,
        )

    def test_renaming_a_lot_re_queues_it(self):
        # The name is the path qualifier: a renamed lot must be re-published
        # so the document appears under the name people actually scan.
        self.capture()
        lot = self._lot("BATCH-952-A", self._published_product())
        lot.openepcis_publish = True
        lot._openepcis_sync()
        self.assertEqual(lot.openepcis_state, "synced")

        lot.name = "BATCH-952-B"
        self.assertEqual(lot.openepcis_state, "queued")


@tagged("post_install", "-at_install")
class TestInstanceDocument(LotCase):
    def test_the_document_names_its_batch(self):
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._published_product())
        lot.openepcis_publish = True
        lot._openepcis_sync()

        payload = calls[0]["payload"]
        self.assertEqual(payload["gtin"], TEST_GTIN_14)
        self.assertEqual(payload["hasBatchLotNumber"], "BATCH-952-A")
        self.assertNotIn("hasSerialNumber", payload)

    def test_the_document_names_its_serial(self):
        calls = self.capture()
        lot = self._lot("952-0007", self._published_product(tracking="serial"))
        lot.openepcis_publish = True
        lot._openepcis_sync()

        payload = calls[0]["payload"]
        self.assertEqual(payload["hasSerialNumber"], "952-0007")
        self.assertNotIn("hasBatchLotNumber", payload)

    def test_the_document_satisfies_the_product_schema(self):
        # The resolver validates instance documents against the full product
        # schema: productName and brand are required at every level, and the
        # shipped mapping reads them across from the product.
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._published_product())
        lot.openepcis_publish = True
        lot._openepcis_sync()

        payload = calls[0]["payload"]
        self.assertEqual(payload["productName"], {"en": "Test product"})
        self.assertEqual(payload["brand"], {"brandName": {"en": "Test brand"}})

    def test_the_creation_date_stands_in_as_production_date(self):
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._published_product())
        lot.openepcis_publish = True
        lot._openepcis_sync()

        self.assertEqual(
            calls[0]["payload"]["productionDate"],
            fields.Date.to_string(lot.create_date),
        )

    def test_instance_records_report_no_missing_registry_terms(self):
        # Registry requirements gate the class-level product; repeating them
        # on every batch would be a permanent warning nothing can satisfy.
        lot = self._lot("BATCH-952-A", self._published_product())
        lot.openepcis_publish = True
        self.assertFalse(lot.openepcis_missing_terms)


@tagged("post_install", "-at_install")
class TestProductGate(LotCase):
    def test_a_lot_waits_while_its_product_is_unpublished(self):
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._tracked_product())
        lot.openepcis_publish = True

        self.assertEqual(lot.openepcis_state, "not_synced", "waiting, not queued")
        self.assertIn("not in the catalog yet", lot.openepcis_product_notice)
        self.assertEqual(calls, [], "nothing may be published under an absent GTIN")

    def test_publish_now_names_the_product_as_the_blocker(self):
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._tracked_product())

        with self.assertRaises(UserError) as caught:
            lot.action_openepcis_publish()
        self.assertIn("Publish it first", str(caught.exception))
        self.assertEqual(calls, [])

    def test_an_invalid_product_gtin_blocks_the_lot(self):
        calls = self.capture()
        product = self._published_product(barcode="9520000000007")  # bad check digit
        lot = self._lot("BATCH-952-A", product)
        lot.openepcis_publish = True

        self.assertEqual(lot.openepcis_state, "not_synced")
        self.assertIn("no valid GTIN", lot.openepcis_product_notice)
        self.assertEqual(calls, [])

    def test_a_waiting_lot_follows_when_the_product_lands(self):
        calls = self.capture()
        product = self._tracked_product()
        lot = self._lot("BATCH-952-A", product)
        lot.openepcis_publish = True
        self.assertEqual(lot.openepcis_state, "not_synced")

        product.openepcis_publish = True
        product._openepcis_sync()
        self.assertEqual(lot.openepcis_state, "queued", "released by the product")

        lot._openepcis_sync()
        self.assertEqual(lot.openepcis_state, "synced")
        self.assertEqual(
            [call["path"] for call in calls],
            ["/products/%s" % TEST_GTIN_14, "/products/%s/10/BATCH-952-A" % TEST_GTIN_14],
            "the product goes first, the instance follows",
        )
