# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""A sale is not a shipment, and the only place that is written down is the till.

Stock cannot tell the two apart: a point-of-sale order leaves through an
ordinary outgoing operation. Reporting it as ``shipping`` is a true statement
about the stock and a wrong one about the goods — nothing is in transit,
somebody bought it and walked out.
"""

from odoo.addons.openepcis_connector_events.tests.common import EventCase
from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestTill(EventCase):
    def _till(self, picking_type):
        return self.env["pos.config"].create(
            {"name": "Ladenkasse", "picking_type_id": picking_type.id}
        )

    def test_an_operation_type_becomes_a_sale_when_a_till_points_at_it(self):
        """The trigger that was missing: nothing on the operation type changes.

        The seeding recomputes on the operation's own codes, and becoming a
        till changes none of them — so the override was correct and unreached.
        """
        picking_type = self._outgoing_type()
        self.assertEqual(picking_type.openepcis_biz_step, "shipping")

        self._till(picking_type)

        self.assertEqual(picking_type.openepcis_biz_step, "retail_selling")
        self.assertEqual(picking_type.openepcis_disposition, "retail_sold")

    def test_a_step_somebody_chose_is_left_alone(self):
        """A value that is not the seed is a decision, and not ours to revise."""
        picking_type = self._outgoing_type()
        picking_type.openepcis_biz_step = "inspecting"

        self._till(picking_type)

        self.assertEqual(picking_type.openepcis_biz_step, "inspecting")

    def test_a_sale_is_reported_as_a_sale(self):
        """The whole point, seen from the event rather than from the setting."""
        picking_type = self._outgoing_type()
        self._till(picking_type)
        product = self._published_product(tracking="none")
        product.product_tmpl_id.is_storable = True
        self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, 10)

        self._transfer(picking_type, product, quantity=1)

        import json

        event = json.loads(self._queued().payload)["epcisBody"]["eventList"][0]
        self.assertEqual(event["bizStep"], "retail_selling")
        self.assertEqual(event["disposition"], "retail_sold")

    def test_an_ordinary_delivery_is_still_a_shipment(self):
        """Only the operation a till points at changes meaning."""
        picking_type = self._outgoing_type()
        self._till(self._incoming_type())

        self.assertEqual(picking_type.openepcis_biz_step, "shipping")

    def test_pointing_the_till_elsewhere_gives_the_old_operation_back(self):
        """A loading bay that stopped being a till is a loading bay again."""
        till_type = self._outgoing_type()
        config = self._till(till_type)
        self.assertEqual(till_type.openepcis_biz_step, "retail_selling")

        config.picking_type_id = self._internal_type()

        self.assertEqual(till_type.openepcis_biz_step, "shipping")
