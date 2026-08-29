# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""What a validated transfer says, and what it refuses to say.

These pin the readings that make an event trustworthy: a lot is a class and a
serial an instance, an event names where it happened or is not made at all,
nothing is claimed about an identifier the catalog does not hold, and the same
movement reported twice is one event.
"""

from odoo.tests import tagged

from .common import TEST_GLN, TEST_GTIN, TEST_PARTNER_GLN, EventCase, Outcome


@tagged("post_install", "-at_install")
class TestWhatIsReported(EventCase):
    def test_a_receipt_becomes_one_event_at_the_receiving_step(self):
        product = self._published_product(tracking="none")
        self._transfer(self._incoming_type(), product)

        events = self._queued()
        self.assertEqual(len(events), 1)
        self.assertEqual(events.biz_step, "receiving")
        self.assertEqual(events.state, "queued")

    def test_a_serial_is_an_instance_and_lands_in_the_epc_list(self):
        product = self._published_product(tracking="serial")
        self._transfer(self._incoming_type(), product, quantity=1, lot_name="952-0007")

        event = self._event()
        self.assertEqual(event["epcList"], ["https://id.gs1.org/01/%s/21/952-0007" % TEST_GTIN])
        self.assertNotIn("quantityList", event)

    def test_a_lot_is_a_class_and_lands_in_the_quantity_list(self):
        # The single most common way to build an event that validates and means
        # the wrong thing is to put an LGTIN in the epcList.
        product = self._published_product(tracking="lot")
        self._transfer(self._incoming_type(), product, quantity=12, lot_name="BATCH-A")

        event = self._event()
        self.assertNotIn("epcList", event)
        self.assertEqual(
            event["quantityList"],
            [{"epcClass": "https://id.gs1.org/01/%s/10/BATCH-A" % TEST_GTIN, "quantity": 12}],
        )

    def test_untracked_goods_are_reported_as_the_trade_item_itself(self):
        # A warehouse that tracks nothing still produces useful events: the
        # class moved, even if no unit of it can be named.
        product = self._published_product(tracking="none")
        self._transfer(self._incoming_type(), product, quantity=5)

        event = self._event()
        self.assertEqual(
            event["quantityList"],
            [{"epcClass": "https://id.gs1.org/01/%s" % TEST_GTIN, "quantity": 5}],
        )

    def test_an_unpublished_product_is_left_out_entirely(self):
        # An identifier that resolves nowhere is not worth an event: a scan
        # would find the event and no product.
        product = self._tracked_product(tracking="none")
        self._transfer(self._incoming_type(), product)

        self.assertFalse(self._queued())

    def _event(self):
        events = self._queued()
        self.assertEqual(len(events), 1, "expected exactly one event")
        import json

        return json.loads(events.payload)["epcisBody"]["eventList"][0]


@tagged("post_install", "-at_install")
class TestWhereItHappened(EventCase):
    def test_a_receipt_is_observed_where_the_goods_arrived(self):
        product = self._published_product(tracking="none")
        self._transfer(self._incoming_type(), product)

        self.assertEqual(self._read_point(), "https://id.gs1.org/414/%s" % TEST_GLN)

    def test_a_sub_location_inherits_the_gln_above_it(self):
        shelf = self.env["stock.location"].create(
            {"name": "Shelf 1", "location_id": self.warehouse.lot_stock_id.id}
        )
        self.assertEqual(shelf._openepcis_read_point(), TEST_GLN)

    def test_without_a_gln_anywhere_nothing_is_reported_and_it_is_said_so(self):
        # Half an event is worse than none: it looks complete.
        self.warehouse.lot_stock_id.openepcis_gln = False
        product = self._published_product(tracking="none")
        picking = self._transfer(self._incoming_type(), product)

        self.assertFalse(self._queued())
        self.assertTrue(
            any("GLN" in (message.body or "") for message in picking.message_ids),
            "the transfer should say why it reported nothing",
        )

    def _read_point(self):
        import json

        return json.loads(self._queued().payload)["epcisBody"]["eventList"][0]["readPoint"]["id"]


@tagged("post_install", "-at_install")
class TestPaperwork(EventCase):
    def test_a_delivery_with_an_address_links_to_the_document_itself(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://odoo.example.test"
        )
        product = self._published_product(tracking="none")
        product.product_tmpl_id.is_storable = True
        self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, 10)
        picking = self._transfer(self._outgoing_type(), product)

        import json

        event = json.loads(self._queued().payload)["epcisBody"]["eventList"][0]
        self.assertEqual(
            event["bizTransactionList"][0]["bizTransaction"],
            "https://odoo.example.test/odoo/stock.picking/%d" % picking.id,
        )

    def test_a_delivery_carries_its_despatch_advice_and_the_customer(self):
        partner = self.env["res.partner"].create(
            {"name": "A customer", "openepcis_gln": TEST_PARTNER_GLN}
        )
        product = self._published_product(tracking="none")
        # Odoo 18 lets a consumable carry a lot but not a quant: only a
        # storable product has stock to take out of a warehouse.
        product.product_tmpl_id.is_storable = True
        self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, 10)
        self._transfer(self._outgoing_type(), product, partner=partner)

        import json

        event = json.loads(self._queued().payload)["epcisBody"]["eventList"][0]
        self.assertEqual(event["bizStep"], "shipping")
        # A URL, not a document number: the schema wants a URI, and of the two
        # forms that satisfy it only one leads anywhere.
        self.assertEqual(event["bizTransactionList"][0]["type"], "desadv")
        self.assertTrue(
            event["bizTransactionList"][0]["bizTransaction"].startswith("urn:epcglobal:cbv:bt:"),
            "a test database has no address of its own, so the GLN form is right here",
        )
        self.assertEqual(
            event["destinationList"],
            [
                {
                    "type": "owning_party",
                    "destination": "https://id.gs1.org/414/%s" % TEST_PARTNER_GLN,
                }
            ],
        )


@tagged("post_install", "-at_install")
class TestArming(EventCase):
    def test_nothing_is_reported_while_the_company_is_off(self):
        self.company.openepcis_events_enabled = False
        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        self.assertFalse(self._queued())

    def test_nothing_is_reported_for_an_operation_that_is_not_armed(self):
        picking_type = self._incoming_type()
        picking_type.openepcis_capture = False
        self._transfer(picking_type, self._published_product(tracking="none"))
        self.assertFalse(self._queued())

    def test_an_operation_type_is_seeded_from_what_its_code_implies(self):
        self.assertEqual(self.warehouse.in_type_id.openepcis_biz_step, "receiving")
        self.assertEqual(self.warehouse.out_type_id.openepcis_biz_step, "shipping")

    def test_a_seeded_mapping_is_never_overwritten(self):
        picking_type = self._incoming_type()
        picking_type.openepcis_biz_step = "inspecting"
        picking_type.sequence_code = "IN2"
        self.assertEqual(picking_type.openepcis_biz_step, "inspecting")


@tagged("post_install", "-at_install")
class TestDelivery(EventCase):
    def test_the_queue_sends_and_then_finds_out_what_became_of_it(self):
        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        event = self._queued()

        self.capture.outcome_to_give = Outcome(running=True, success=False)
        self._deliver()
        self.assertEqual(event.state, "accepted", "202 is custody, not validity")

        self.capture.outcome_to_give = Outcome(running=False, success=True)
        self._deliver()
        self.assertEqual(event.state, "captured")

    def test_a_job_the_repository_forgets_never_turns_green(self):
        # It was accepted — that much is true — but "stored" would be a guess,
        # and a refusal answers the same way.
        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        event = self._queued()
        self.capture.outcome_to_give = Outcome(running=False, success=False, known=False)
        for _attempt in range(6):
            self._deliver()

        self.assertEqual(event.state, "accepted")
        self.assertIn("cannot be confirmed", event.error or "")

    def test_a_document_refused_afterwards_keeps_the_reason(self):
        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        event = self._queued()
        self.capture.outcome_to_give = Outcome(
            running=False, success=False, errors=("epcList[0] is not a URI",)
        )
        self._deliver()

        self.assertEqual(event.state, "rejected")
        self.assertIn("not a URI", event.error)

    def test_the_same_movement_reported_twice_is_one_event(self):
        picking = self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        picking._openepcis_report()

        self.assertEqual(len(self._queued()), 1)


@tagged("post_install", "-at_install")
class TestAggregation(EventCase):
    def test_packing_into_a_unit_says_what_is_underneath_it(self):
        product = self._published_product(tracking="serial")
        picking_type = self._incoming_type()
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": self.supplier_location.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": product.name,
                            "product_id": product.id,
                            "product_uom_qty": 1,
                            "product_uom": product.uom_id.id,
                        },
                    )
                ],
            }
        )
        picking.action_confirm()
        picking.action_assign()
        package = self.env["stock.quant.package"].create({})
        for line in picking.move_line_ids:
            line.quantity = 1
            line.lot_name = "952-0100"
            line.result_package_id = package
        picking.move_ids.picked = True
        picking.button_validate()

        import json

        documents = [json.loads(row.payload) for row in self._queued()]
        aggregations = [
            event
            for document in documents
            for event in document["epcisBody"]["eventList"]
            if event["type"] == "AggregationEvent"
        ]
        self.assertEqual(len(aggregations), 1)
        self.assertEqual(
            aggregations[0]["parentID"], "https://id.gs1.org/00/%s" % package.openepcis_sscc
        )
        self.assertEqual(aggregations[0]["bizStep"], "packing")
        self.assertEqual(
            aggregations[0]["childEPCs"],
            ["https://id.gs1.org/01/%s/21/952-0100" % TEST_GTIN],
        )

    def test_a_new_package_is_given_an_sscc_from_the_company_prefix(self):
        package = self.env["stock.quant.package"].create({})
        self.assertTrue(package.openepcis_sscc)
        self.assertTrue(package.openepcis_sscc.startswith("0" + self.company.openepcis_gcp))
