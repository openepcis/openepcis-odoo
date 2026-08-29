# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""What a partner's event is allowed to do to our paperwork.

Two kinds of test live here, and the second kind matters more than the first.

The first kind checks the ladder: that a setting does what it says. Those are
ordinary tests, and if one breaks somebody notices immediately.

The second kind checks the invariant — that no configuration, however set, lets
an event create a document or stock or reopen something closed. Those are the
tests that guard against a plausible future change: somebody widens a domain,
relaxes a state check, adds a convenience, and the connector quietly grows the
ability to write into a valued ledger from a foreign system. They are written
to fail loudly in that case, which is why several of them turn *everything* on
first and then assert that it still did not happen.
"""

from .common import TEST_PARTNER_GLN, EventCase
from .test_inbound import event


class TestInboundPosting(EventCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create(
            {
                "name": "A trusted partner",
                "is_company": True,
                "openepcis_gln": TEST_PARTNER_GLN,
                "openepcis_inbound_trusted": True,
            }
        )
        self.product = self._published_product(tracking="lot")
        # Every test here needs a transfer that is genuinely reserved, and a
        # reservation needs stock. Odoo refuses a quant for a consumable, which
        # is what a product is unless it is told otherwise.
        self.product.product_tmpl_id.is_storable = True
        self.lot = self._lot("LOT-952-P1", self.product)
        self.identifier = "https://id.gs1.org/01/%s/10/%s" % (
            self.product.barcode,
            self.lot.name,
        )

    # -- scaffolding ----------------------------------------------------

    def _arm(self, picking_type, policy="post", observe=False):
        picking_type.write(
            {
                "openepcis_inbound_policy": policy,
                "openepcis_inbound_observe": observe,
            }
        )

    def _stock_the_lot(self, quantity=5):
        self.env["stock.quant"]._update_available_quantity(
            self.product, self.warehouse.lot_stock_id, quantity, lot_id=self.lot
        )

    def _waiting_delivery(self, partner=None):
        self._stock_the_lot()
        picking_type = self._outgoing_type()
        picking = self._open_transfer(
            picking_type,
            self.product,
            quantity=1,
            lot=self.lot,
            partner=partner if partner is not None else self.partner,
        )
        return picking_type, picking

    # "receiving" and not "shipping": these are deliveries of ours, and the step
    # that attests one completed is the partner's receipt. His despatch note says
    # the goods left him, which ATTESTING_STEPS deliberately does not accept.
    def _hear(self, uuid="urn:uuid:p1", biz_step="receiving", party=TEST_PARTNER_GLN):
        self.query.pages = [[event(uuid, biz_step=biz_step, epc=self.identifier, party=party)]]
        self.env["openepcis.inbound.event"]._cron_poll()
        return self.env["openepcis.inbound.event"].search([("event_uuid", "=", uuid)])

    # -- the ladder -----------------------------------------------------

    def test_by_default_an_event_is_shown_and_nothing_is_posted(self):
        """The setting somebody never touches has to be the safe one."""
        picking_type, picking = self._waiting_delivery()
        self.assertEqual(picking_type.openepcis_inbound_policy, "show")

        row = self._hear()

        self.assertEqual(picking.state, "assigned")
        self.assertEqual(row.state, "posted", "shown, as before")

    def test_armed_to_post_it_posts_the_transfer_it_was_waiting_for(self):
        picking_type, picking = self._waiting_delivery()
        self._arm(picking_type)

        row = self._hear()

        self.assertEqual(picking.state, "done")
        self.assertEqual(row.state, "booked")
        self.assertIn(TEST_PARTNER_GLN, row.posting_note or "")

    def test_while_observing_it_decides_everything_and_posts_nothing(self):
        """The rehearsal has to reach the same verdict, or it proves nothing."""
        picking_type, picking = self._waiting_delivery()
        self._arm(picking_type, observe=True)

        row = self._hear()

        self.assertEqual(picking.state, "assigned", "observed, not posted")
        self.assertEqual(row.state, "proposed")
        self.assertTrue(row.observed_only)
        self.assertIn("Would have posted", row.posting_note or "")

    def test_set_to_propose_it_asks_instead_of_acting(self):
        picking_type, picking = self._waiting_delivery()
        self._arm(picking_type, policy="propose")

        row = self._hear()

        self.assertEqual(picking.state, "assigned")
        self.assertEqual(row.state, "proposed")
        self.assertFalse(row.observed_only, "nobody was rehearsing; this is a real question")

    def test_both_settings_have_to_agree(self):
        """The operation type alone is not a permission, and neither is the partner."""
        picking_type, picking = self._waiting_delivery()
        self._arm(picking_type)
        self.partner.openepcis_inbound_trusted = False

        row = self._hear()

        self.assertEqual(picking.state, "assigned")
        self.assertIn("not allowed", row.posting_note or "")

    def test_an_event_from_somebody_else_does_not_post_our_partners_transfer(self):
        """Attribution is the whole basis of the permission."""
        picking_type, picking = self._waiting_delivery()
        self._arm(picking_type)
        stranger = "9520000000042"

        row = self._hear(party=stranger)

        self.assertEqual(picking.state, "assigned")
        self.assertIn("reported by", row.posting_note or "")

    def test_a_step_that_does_not_mean_completion_does_not_complete_anything(self):
        picking_type, picking = self._waiting_delivery()
        self._arm(picking_type)

        row = self._hear(biz_step="inspecting")

        self.assertEqual(picking.state, "assigned")
        self.assertIn("does not attest", row.posting_note or "")

    def test_a_transfer_without_a_partner_is_never_posted(self):
        picking_type, picking = self._waiting_delivery(partner=False)
        self._arm(picking_type)

        row = self._hear()

        self.assertEqual(picking.state, "assigned")
        self.assertIn("no partner", row.posting_note or "")

    def test_two_open_transfers_for_the_same_lot_are_an_ambiguity_not_a_guess(self):
        picking_type, first = self._waiting_delivery()
        self._arm(picking_type)
        second = self._open_transfer(
            picking_type, self.product, quantity=1, lot=self.lot, partner=self.partner
        )

        self._hear()

        self.assertEqual(first.state, "assigned")
        self.assertEqual(second.state, "assigned")

    def test_a_receipt_of_ours_is_never_closed_by_a_partners_despatch(self):
        """That the goods left him is not that they arrived here.

        The tempting reading — supplier says shipped, so book the receipt — puts
        stock on our books for a lorry that is still on the road. Nobody outside
        can witness an arrival at our dock, so nothing outside may close it.
        """
        picking_type = self._incoming_type()
        self._arm(picking_type)
        picking = self._open_transfer(picking_type, self.product, quantity=1, partner=self.partner)

        row = self._hear(biz_step="shipping")

        self.assertNotEqual(picking.state, "done")
        self.assertNotEqual(row.state, "booked")

    # -- the invariant --------------------------------------------------

    def test_no_setting_lets_an_event_create_a_transfer(self):
        """Fully armed, with nothing waiting: the event must land as news only."""
        self._arm(self._outgoing_type())
        self._arm(self._incoming_type())
        before = self.env["stock.picking"].search_count([])

        row = self._hear()

        self.assertEqual(self.env["stock.picking"].search_count([]), before)
        self.assertEqual(row.state, "posted", "shown on the lot, and that is all")

    def _on_hand(self):
        return sum(
            self.env["stock.quant"].search([("lot_id", "=", self.lot.id)]).mapped("quantity")
        )

    def test_no_setting_lets_an_event_create_stock(self):
        self._arm(self._incoming_type())
        before = self._on_hand()

        self._hear(biz_step="receiving")

        self.assertEqual(self._on_hand(), before, "an observation is not a document")

    def test_no_setting_lets_an_event_reopen_a_finished_transfer(self):
        self._stock_the_lot()
        picking_type = self._outgoing_type()
        self._arm(picking_type)
        picking = self._open_transfer(
            picking_type, self.product, quantity=1, lot=self.lot, partner=self.partner
        )
        picking.button_validate()
        self.assertEqual(picking.state, "done")

        row = self._hear()

        self.assertEqual(picking.state, "done")
        self.assertNotEqual(row.state, "booked")

    def test_an_unreserved_transfer_is_not_advanced(self):
        """Reserved is what makes the movement real; confirmed alone is a wish."""
        picking_type = self._outgoing_type()
        self._arm(picking_type)
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": self.warehouse.lot_stock_id.id,
                "location_dest_id": self.customer_location.id,
                "partner_id": self.partner.id,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": self.product.name,
                            "product_id": self.product.id,
                            "product_uom_qty": 1,
                            "product_uom": self.product.uom_id.id,
                        },
                    )
                ],
            }
        )
        picking.action_confirm()

        self._hear()

        self.assertNotEqual(picking.state, "done")


class TestInboundScope(EventCase):
    """Which events are kept at all — a convenience, never a permission."""

    def _read(self, identifier, scope):
        self.company.openepcis_inbound_scope = scope
        self.query.pages = [[event("urn:uuid:s1", epc=identifier)]]
        self.env["openepcis.inbound.event"]._cron_poll()
        return self.env["openepcis.inbound.event"].search([])

    def test_everything_is_the_default(self):
        rows = self._read("https://id.gs1.org/01/04012345123456/10/L1", "all")
        self.assertEqual(len(rows), 1)

    def test_narrowed_to_our_prefix_a_strangers_goods_are_dropped(self):
        rows = self._read("https://id.gs1.org/01/04012345123456/10/L1", "own_gcp")
        self.assertFalse(rows)

    def test_narrowed_to_our_prefix_our_own_goods_are_kept(self):
        product = self._published_product(tracking="lot")
        identifier = "https://id.gs1.org/01/%s/10/L1" % product.barcode
        self.company.openepcis_gcp = product.barcode[:7]
        rows = self._read(identifier, "own_gcp")
        self.assertEqual(len(rows), 1)

    def test_narrowing_without_a_prefix_keeps_everything_rather_than_nothing(self):
        """A half-made setting must not quietly empty the inbox."""
        self.company.openepcis_gcp = False
        rows = self._read("https://id.gs1.org/01/04012345123456/10/L1", "own_gcp")
        self.assertEqual(len(rows), 1, "an empty inbox looks like a broken connector")

    def test_dropped_events_still_move_the_watermark(self):
        """Otherwise a narrowed inbox re-reads the same rejected window forever."""
        self._read("https://id.gs1.org/01/04012345123456/10/L1", "own_gcp")
        self.assertTrue(self.company.openepcis_events_since)


class TestInboundLimits(EventCase):
    """The two brakes on a single run."""

    def test_the_batch_size_is_the_companys_to_set(self):
        self.company.openepcis_inbound_batch = 2
        self.query.pages = [[event("urn:uuid:b%d" % n) for n in range(5)]]

        self.env["openepcis.inbound.event"]._cron_poll()

        self.assertEqual(len(self.env["openepcis.inbound.event"].search([])), 2)

    def test_an_unset_limit_falls_back_rather_than_reading_nothing(self):
        self.company.openepcis_inbound_batch = 0
        self.query.pages = [[event("urn:uuid:b%d" % n) for n in range(3)]]

        self.env["openepcis.inbound.event"]._cron_poll()

        self.assertEqual(len(self.env["openepcis.inbound.event"].search([])), 3)

    def test_the_page_limit_reaches_the_query(self):
        self.company.openepcis_inbound_pages = 3
        seen = {}
        original = self.query.since

        def remember(since="", per_page=100, pages=50):
            seen["pages"] = pages
            return original(since, per_page, pages)

        self.query.since = remember
        self.query.pages = [[event("urn:uuid:b1")]]

        self.env["openepcis.inbound.event"]._cron_poll()

        self.assertEqual(seen["pages"], 3)
