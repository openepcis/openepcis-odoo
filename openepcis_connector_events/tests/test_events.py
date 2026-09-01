# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""What a validated transfer says, and what it refuses to say.

These pin the readings that make an event trustworthy: a lot is a class and a
serial an instance, an event names where it happened or is not made at all,
nothing is claimed about an identifier the catalog does not hold, and the same
movement reported twice is one event.
"""

from odoo.tests import tagged

from .common import TEST_GLN, TEST_GTIN, TEST_PARTNER_GLN, EventCase, Outcome

#: TEST_GTIN as AI 01 spells it. Written out rather than derived from
#: TEST_GTIN, because deriving it is how the missing padding stayed invisible:
#: a test that computes its expectation the way the code does agrees with the
#: code whatever the code says. This is the form GS1 prescribes, and a live
#: repository refuses anything else ("Translation failed").
TEST_GTIN_14 = "09520000000004"


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
        self.assertEqual(event["epcList"], ["https://id.gs1.org/01/%s/21/952-0007" % TEST_GTIN_14])
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
            [{"epcClass": "https://id.gs1.org/01/%s/10/BATCH-A" % TEST_GTIN_14, "quantity": 12}],
        )

    def test_the_barcode_is_an_ean13_and_the_event_carries_fourteen_digits(self):
        """AI 01 is fourteen digits, whatever the label says.

        The product carries an ordinary EAN-13 — that is the normal case in a
        warehouse — and the event has to carry the padded form. An event with
        thirteen digits is accepted by the capture endpoint with a 202 and then
        refused by the repository with "Translation failed"; measured against a
        live repository from a running Odoo on 2026-08-31.
        """
        self.assertEqual(len(TEST_GTIN), 13, "the label is an EAN-13")
        self.assertEqual(TEST_GTIN_14, "0" + TEST_GTIN)

        product = self._published_product(tracking="none")
        self.assertEqual(product.barcode, TEST_GTIN)
        self._transfer(self._incoming_type(), product, quantity=1)

        epc_class = self._event()["quantityList"][0]["epcClass"]
        self.assertTrue(epc_class.endswith("/01/" + TEST_GTIN_14), epc_class)

    def test_untracked_goods_are_reported_as_the_trade_item_itself(self):
        # A warehouse that tracks nothing still produces useful events: the
        # class moved, even if no unit of it can be named.
        product = self._published_product(tracking="none")
        self._transfer(self._incoming_type(), product, quantity=5)

        event = self._event()
        self.assertEqual(
            event["quantityList"],
            [{"epcClass": "https://id.gs1.org/01/%s" % TEST_GTIN_14, "quantity": 5}],
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

    def test_without_a_completion_time_nothing_is_reported_and_it_is_said_so(self):
        """An invented event time is a false statement with an identity.

        The event time goes into the canonical event hash, which is the event's
        identity. Standing in the moment of reporting would therefore not only
        misstate when the goods moved — it would give the same movement a
        different identity every time it was sent.
        """
        product = self._published_product(tracking="none")
        picking = self._transfer(self._incoming_type(), product)
        already_queued = self._queued()
        # Odoo stamps date_done in _action_done, so this is the shape that is
        # left over: a transfer that is done and cannot say when.
        picking.write({"date_done": False})

        picking._openepcis_report()

        self.assertEqual(self._queued(), already_queued, "nothing new was queued")
        self.assertTrue(
            any("when it happened" in (message.body or "") for message in picking.message_ids),
            "the transfer should say why it reported nothing",
        )

    def test_the_event_time_is_the_completion_time_not_the_reporting_time(self):
        import json
        from datetime import datetime

        product = self._published_product(tracking="none")
        picking = self._transfer(self._incoming_type(), product)
        # Report again from scratch, with a completion time that is nowhere
        # near now: the queue recognises a movement it already holds, so the
        # first row has to go before the second report can say anything.
        self._queued().sudo().unlink()
        picking.write({"date_done": datetime(2026, 8, 10, 9, 0, 0)})

        picking._openepcis_report()

        event = json.loads(self._queued().payload)["epcisBody"]["eventList"][0]
        self.assertEqual(event["eventTime"], "2026-08-10T09:00:00.000Z")

    def _read_point(self):
        import json

        return json.loads(self._queued().payload)["epcisBody"]["eventList"][0]["readPoint"]["id"]


@tagged("post_install", "-at_install")
class TestPaperwork(EventCase):
    def test_the_paperwork_is_named_the_same_way_from_every_deployment(self):
        """The reference is part of the event's identity, so it may not carry a host.

        A URL would be the more useful of the two forms the schema allows — it
        can be followed. But the reference goes into the canonical hash, and a
        deployment's address in the identity means the same movement gets two
        names depending on where it was reported from. Setting web.base.url
        must therefore change nothing here.
        """
        self.env["ir.config_parameter"].sudo().set_param(
            "web.base.url", "https://odoo.example.test"
        )
        product = self._published_product(tracking="none")
        product.product_tmpl_id.is_storable = True
        self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, 10)
        picking = self._transfer(self._outgoing_type(), product)

        import json

        event = json.loads(self._queued().payload)["epcisBody"]["eventList"][0]
        reference = event["bizTransactionList"][0]["bizTransaction"]
        self.assertNotIn("odoo.example.test", reference)
        self.assertEqual(
            reference,
            "urn:epcglobal:cbv:bt:%s:%s" % (TEST_GLN, picking.name.replace("/", "%2F")),
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

    def test_the_document_leaves_without_an_event_id(self):
        """One canonicalisation, and it is the repository's.

        Two implementations of the hash exist and they do not agree on every
        event shape. Sending an identifier we computed would put our answer
        into somebody else's repository permanently; leaving the field empty
        lets the side that stores the event name it.
        """
        import json

        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        row = self._queued()
        event = json.loads(row.payload)["epcisBody"]["eventList"][0]

        self.assertNotIn("eventID", event)
        # …but the row knows what to expect, for recognising the echo later.
        self.assertTrue(row.event_hash.startswith("ni:///sha-256;"))
        self.assertTrue(row.idem_key.startswith("urn:uuid:"))

    def test_an_event_the_repository_already_holds_counts_as_captured(self):
        """A duplicate is the answer we were hoping for, not a failure.

        It means the first attempt landed after all. Booking it as refused
        would leave the queue full of rows that look broken and are not.
        """
        from odoo.addons.openepcis_connector.vendor.benelog_client.core.errors import (
            BenelogError,
        )

        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        self.capture.error_to_raise = BenelogError("Duplicate EPCIS Event", status=400)

        self._deliver()

        self.assertEqual(self._queued().state, "captured")
        self.assertFalse(self._queued().error)


@tagged("post_install", "-at_install")
class TestReturns(EventCase):
    """What a return says about the shipment the goods went out on."""

    def _delivery(self, product, partner):
        self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, 5)
        return self._transfer(self._outgoing_type(), product, quantity=2, partner=partner)

    def _return_of(self, delivery):
        """The return Odoo's own wizard would build."""
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": self._incoming_type().id,
                "location_id": self.customer_location.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
                "partner_id": delivery.partner_id.id,
                "origin": "Return of %s" % delivery.name,
                "return_id": delivery.id,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": move.product_id.name,
                            "product_id": move.product_id.id,
                            "product_uom_qty": 2,
                            "product_uom": move.product_uom.id,
                        },
                    )
                    for move in delivery.move_ids
                ],
            }
        )
        picking.action_confirm()
        picking.action_assign()
        for line in picking.move_line_ids:
            line.quantity = 2
        picking.move_ids.picked = True
        picking.button_validate()
        return picking

    def _events_of_type(self, kind):
        import json

        return [
            event
            for row in self._queued()
            for event in json.loads(row.payload)["epcisBody"]["eventList"]
            if event["type"] == kind
        ]

    def test_returned_goods_leave_the_shipment_they_went_out_on(self):
        """A receipt says they arrived, not that they left the despatch advice.

        The association is a standing statement: until something withdraws it,
        the shipment answers with goods that have long since come back.
        """
        partner = self.env["res.partner"].create(
            {"name": "A customer", "openepcis_gln": TEST_PARTNER_GLN}
        )
        product = self._published_product(tracking="none")
        product.product_tmpl_id.is_storable = True
        delivery = self._delivery(product, partner)
        shipped_under = self._events_of_type("ObjectEvent")[0]["bizTransactionList"]
        self._queued().sudo().unlink()

        self._return_of(delivery)

        releases = self._events_of_type("TransactionEvent")
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0]["action"], "DELETE")
        self.assertEqual(releases[0]["disposition"], "returned")
        # The very document the goods went out under, not the return's own.
        self.assertEqual(releases[0]["bizTransactionList"], shipped_under)

    def test_the_arrival_is_still_reported_as_its_own_event(self):
        """Two different statements, and the return makes both."""
        partner = self.env["res.partner"].create(
            {"name": "A customer", "openepcis_gln": TEST_PARTNER_GLN}
        )
        product = self._published_product(tracking="none")
        product.product_tmpl_id.is_storable = True
        delivery = self._delivery(product, partner)
        self._queued().sudo().unlink()

        self._return_of(delivery)

        self.assertEqual(len(self._events_of_type("ObjectEvent")), 1)
        self.assertEqual(len(self._events_of_type("TransactionEvent")), 1)

    def test_an_ordinary_receipt_releases_nothing(self):
        """Only a return releases. A receipt is not a return of anything."""
        product = self._published_product(tracking="none")
        self._transfer(self._incoming_type(), product)

        self.assertEqual(self._events_of_type("TransactionEvent"), [])

    def test_a_return_of_paperless_goods_releases_nothing(self):
        """An internal transfer names no document, so there is nothing to leave."""
        product = self._published_product(tracking="none")
        product.product_tmpl_id.is_storable = True
        self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, 5)
        internal = self._transfer(self._internal_type(), product, quantity=1)
        self._queued().sudo().unlink()

        self._return_of(internal)

        self.assertEqual(self._events_of_type("TransactionEvent"), [])


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
            ["https://id.gs1.org/01/%s/21/952-0100" % TEST_GTIN_14],
        )

    def _aggregations(self):
        import json

        return [
            event
            for row in self._queued()
            for event in json.loads(row.payload)["epcisBody"]["eventList"]
            if event["type"] == "AggregationEvent"
        ]

    def _stocked_serial(self, product, serial, package):
        """One serial of ``product``, in stock and inside ``package``."""
        picking = self._transfer_into_package(product, serial, package)
        self._queued().sudo().unlink()
        picking.message_ids.unlink()
        return picking

    def _transfer_into_package(self, product, serial, package):
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": self._incoming_type().id,
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
        for line in picking.move_line_ids:
            line.quantity = 1
            line.lot_name = serial
            line.result_package_id = package
        picking.move_ids.picked = True
        picking.button_validate()
        return picking

    def test_picking_goods_off_a_pallet_says_the_unit_lost_them(self):
        """An aggregation is a standing statement, so it has to be withdrawn.

        Scan the SSCC and the repository answers what is underneath it. A unit
        that is emptied and never says so keeps answering with goods that are
        no longer in it — and the operation types could be labelled
        ``unpacking`` all along, which made the missing half easy to overlook.
        """
        product = self._published_product(tracking="serial")
        product.product_tmpl_id.is_storable = True
        package = self.env["stock.quant.package"].create({})
        self._stocked_serial(product, "952-0200", package)

        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": self._outgoing_type().id,
                "location_id": self.warehouse.lot_stock_id.id,
                "location_dest_id": self.customer_location.id,
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
        # Off the pallet and into the van: the goods come out of the unit and
        # go into none. That is what a picker does, and what the move line
        # says afterwards.
        for line in picking.move_line_ids:
            line.quantity = 1
            line.package_id = package
            line.result_package_id = False
        picking.move_ids.picked = True
        picking.button_validate()

        aggregations = self._aggregations()
        self.assertEqual(len(aggregations), 1, [e.get("bizStep") for e in aggregations])
        self.assertEqual(aggregations[0]["action"], "DELETE")
        self.assertEqual(aggregations[0]["bizStep"], "unpacking")
        self.assertEqual(
            aggregations[0]["parentID"], "https://id.gs1.org/00/%s" % package.openepcis_sscc
        )

    def test_the_unpack_button_says_it_too(self):
        """The everyday way of emptying a pallet leaves no transfer at all.

        Odoo has two: picking goods off it in a delivery, which leaves move
        lines, and this button, which clears the package on the quants and is
        done. Reporting only the first would tell the repository about the
        rarer of the two.
        """
        product = self._published_product(tracking="serial")
        product.product_tmpl_id.is_storable = True
        package = self.env["stock.quant.package"].create({})
        self._stocked_serial(product, "952-0202", package)

        package.unpack()

        aggregations = self._aggregations()
        self.assertEqual(len(aggregations), 1, [e.get("bizStep") for e in aggregations])
        self.assertEqual(aggregations[0]["action"], "DELETE")
        self.assertEqual(aggregations[0]["bizStep"], "unpacking")
        self.assertEqual(
            aggregations[0]["parentID"], "https://id.gs1.org/00/%s" % package.openepcis_sscc
        )
        self.assertEqual(
            aggregations[0]["childEPCs"],
            ["https://id.gs1.org/01/%s/21/952-0202" % TEST_GTIN_14],
        )

    def test_unpacking_an_empty_unit_says_nothing(self):
        package = self.env["stock.quant.package"].create({})

        package.unpack()

        self.assertEqual(self._aggregations(), [])

    def test_a_unit_that_only_moves_is_neither_packed_nor_unpacked(self):
        """Reporting a move as a packing would restate what never changed."""
        product = self._published_product(tracking="serial")
        product.product_tmpl_id.is_storable = True
        package = self.env["stock.quant.package"].create({})
        self._stocked_serial(product, "952-0201", package)

        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": self._outgoing_type().id,
                "location_id": self.warehouse.lot_stock_id.id,
                "location_dest_id": self.customer_location.id,
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
        for line in picking.move_line_ids:
            line.quantity = 1
            line.result_package_id = line.package_id
        picking.move_ids.picked = True
        picking.button_validate()

        self.assertEqual(self._aggregations(), [], "the pallet moved, it was not repacked")

    def test_two_units_of_the_same_lot_are_two_events(self):
        """The unit belongs in the outbox key, or the second one disappears.

        Two pallets of the same lot state the same contents. Keyed on the
        contents alone, the second event met the first one's row and was
        dropped without a word.
        """
        product = self._published_product(tracking="lot")
        product.product_tmpl_id.is_storable = True
        first = self.env["stock.quant.package"].create({})
        second = self.env["stock.quant.package"].create({})

        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": self._incoming_type().id,
                "location_id": self.supplier_location.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": product.name,
                            "product_id": product.id,
                            "product_uom_qty": 2,
                            "product_uom": product.uom_id.id,
                        },
                    )
                ],
            }
        )
        picking.action_confirm()
        picking.action_assign()
        line = picking.move_line_ids[0]
        line.quantity = 1
        line.lot_name = "BATCH-TWO-PALLETS"
        line.result_package_id = first
        second_line = line.copy({"quantity": 1, "result_package_id": second.id})
        second_line.lot_name = "BATCH-TWO-PALLETS"
        picking.move_ids.picked = True
        picking.button_validate()

        parents = sorted(event["parentID"] for event in self._aggregations())
        self.assertEqual(
            parents,
            sorted(
                "https://id.gs1.org/00/%s" % package.openepcis_sscc for package in (first, second)
            ),
        )

    def test_a_new_package_is_given_an_sscc_from_the_company_prefix(self):
        package = self.env["stock.quant.package"].create({})
        self.assertTrue(package.openepcis_sscc)
        self.assertTrue(package.openepcis_sscc.startswith("0" + self.company.openepcis_gcp))


@tagged("post_install", "-at_install")
class TestWithdrawal(EventCase):
    """Correcting by declaration: the original stays, a withdrawal joins it."""

    def _captured_row(self):
        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        self._deliver()
        row = self._queued()
        self.assertEqual(row.state, "captured")
        return row

    def test_a_withdrawal_repeats_the_event_with_a_declaration(self):
        import json

        row = self._captured_row()

        row.action_declare_error()

        withdrawal = self.env["openepcis.event"].search([("correction_of", "=", row.id)])
        self.assertTrue(withdrawal)
        self.assertTrue(row.corrected)
        event = json.loads(withdrawal.payload)["epcisBody"]["eventList"][0]
        original = json.loads(row.payload)["epcisBody"]["eventList"][0]
        self.assertEqual(event["errorDeclaration"]["reason"], "did_not_occur")
        self.assertNotIn("correctiveEventIDs", event["errorDeclaration"])
        # Everything else is the event that was reported, unchanged: that is
        # what lets the repository recognise which event is being withdrawn.
        self.assertEqual({k: v for k, v in event.items() if k != "errorDeclaration"}, original)

    def test_the_withdrawal_carries_the_identity_of_what_it_withdraws(self):
        # The declaration fields are outside the canonical hash, so both rows
        # expect the same event id — which is the mechanism, not a coincidence.
        row = self._captured_row()

        row.action_declare_error()

        withdrawal = self.env["openepcis.event"].search([("correction_of", "=", row.id)])
        self.assertEqual(withdrawal.event_hash, row.event_hash)
        self.assertNotEqual(withdrawal.idem_key, row.idem_key)

    def test_nothing_is_withdrawn_before_the_repository_holds_it(self):
        from odoo.exceptions import UserError

        self._transfer(self._incoming_type(), self._published_product(tracking="none"))
        row = self._queued()
        self.assertEqual(row.state, "queued")

        with self.assertRaises(UserError):
            row.action_declare_error()

    def test_an_event_is_withdrawn_once(self):
        from odoo.exceptions import UserError

        row = self._captured_row()
        row.action_declare_error()

        with self.assertRaises(UserError):
            row.action_declare_error()
