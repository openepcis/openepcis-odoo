# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""A production run, and the claim that carries across it.

The interesting assertions here are not that fields are filled in but that the
two halves travel together: an event naming only the flour, or only the bread,
is not a smaller statement than one naming both — it is a different and false
one.
"""

import json

from odoo.addons.openepcis_connector_events.tests.common import (
    TEST_GCP,
    TEST_GLN,
    TEST_GTIN,
    TEST_GTIN_2,
    EventCase,
)
from odoo.tests import tagged

#: The two barcodes as AI 01 spells them. Written out rather than derived: a
#: test that computes its expectation the way the code does agrees with the
#: code whatever the code says.
FLOUR_GTIN_14 = "0" + TEST_GTIN
BREAD_GTIN_14 = "0" + TEST_GTIN_2


@tagged("post_install", "-at_install")
class TestManufacturing(EventCase):
    def setUp(self):
        super().setUp()
        self.production_location = self.env.ref("stock.stock_location_stock")
        # Two products, two barcodes: Odoo refuses a second product with a
        # barcode another one already carries, and rightly so.
        self.flour = self._published_product(tracking="lot", name="Mehl Type 550")
        self.bread = self._published_product(tracking="lot", name="Bauernbrot", barcode=TEST_GTIN_2)

    def _published_product(self, tracking="lot", name=None, **values):
        product = super()._published_product(tracking=tracking, **values)
        if name:
            product.name = name
        product.product_tmpl_id.is_storable = True
        return product

    def _run(self, quantity=1.0, with_components=True):
        """A manufacturing order, taken to done the way a plant would."""
        production = self.env["mrp.production"].create(
            {
                "product_id": self.bread.id,
                "product_uom_id": self.bread.uom_id.id,
                "product_qty": quantity,
                "location_src_id": self.production_location.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
            }
        )
        if with_components:
            # Both products are lot-tracked, so Odoo insists on knowing which
            # flour went in and which loaf came out — which is exactly what
            # makes the event worth sending.
            self.env["stock.quant"]._update_available_quantity(
                self.flour,
                self.production_location,
                100,
                lot_id=self._lot("FLOUR-7", self.flour),
            )
            self.env["stock.move"].create(
                {
                    "name": self.flour.name,
                    "product_id": self.flour.id,
                    "product_uom": self.flour.uom_id.id,
                    "product_uom_qty": 25,
                    "location_id": self.production_location.id,
                    "location_dest_id": self.env.ref("stock.stock_location_stock").id,
                    "raw_material_production_id": production.id,
                }
            )
        production.action_confirm()
        production.action_assign()
        # Odoo 19 turned the produced lot into a Many2many, lot_producing_ids;
        # the 18.0 branch assigns the singular lot_producing_id.
        production.lot_producing_ids = self._lot("LOAF-1", self.bread)
        production.qty_producing = quantity
        production._set_qty_producing()
        production.button_mark_done()
        return production

    def _transformations(self):
        return [
            event
            for row in self._queued()
            for event in json.loads(row.payload)["epcisBody"]["eventList"]
            if event["type"] == "TransformationEvent"
        ]

    def test_a_finished_run_says_what_became_of_what(self):
        self._run()

        events = self._transformations()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIn("inputQuantityList", event)
        self.assertIn("outputQuantityList", event)
        self.assertTrue(
            event["inputQuantityList"][0]["epcClass"].startswith(
                "https://id.gs1.org/01/%s" % FLOUR_GTIN_14
            ),
            event["inputQuantityList"],
        )
        self.assertTrue(
            event["outputQuantityList"][0]["epcClass"].startswith(
                "https://id.gs1.org/01/%s" % BREAD_GTIN_14
            ),
            event["outputQuantityList"],
        )
        self.assertEqual(event["readPoint"], {"id": "https://id.gs1.org/414/%s" % TEST_GLN})

    def test_a_transformation_carries_no_action(self):
        """Both halves are true at once, so EPCIS leaves the field out."""
        self._run()

        self.assertNotIn("action", self._transformations()[0])

    def test_a_run_without_components_reports_nothing(self):
        """Half a transformation would claim the bread came from nothing."""
        self._run(with_components=False)

        self.assertEqual(self._transformations(), [])

    def test_the_same_run_reported_twice_is_one_event(self):
        production = self._run()
        production._openepcis_report_transformation()

        self.assertEqual(len(self._transformations()), 1)

    def test_nothing_is_reported_while_the_operation_type_is_off(self):
        self.env["stock.picking.type"].search(
            [("code", "=", "mrp_operation")]
        ).openepcis_capture = False

        self._run()

        self.assertEqual(self._transformations(), [])

    def test_the_company_prefix_is_the_test_range(self):
        # Guard rail for the fixtures rather than for the code: an identifier
        # outside 952 would collide with a real company's numbers.
        self.assertTrue(self.company.openepcis_gcp.startswith("952"), TEST_GCP)
