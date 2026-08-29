# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Scaffolding: an armed company, places with GLNs, and a repository that is not there.

The capture service is stubbed at the seam where the addon reaches for it, so
no test touches a network — and the stub records documents, because what these
tests are about is what was said, not that something was sent.
"""

from unittest.mock import patch

from odoo.addons.openepcis_connector_stock.tests.common import TEST_GTIN, LotCase

# The class that *defines* the method, not the one the model is named after:
# Odoo merges both into one model, and a patch has to land where the
# attribute actually lives.
from ..models.openepcis_client import OpenepcisClient

# GS1 reserves 952 for testing; a location number from that range can never
# collide with a real place.
TEST_GLN = "9520000000011"
TEST_GLN_DOCK = "9520000000028"
TEST_PARTNER_GLN = "9520000000035"

# A prefix from the same range, short enough to leave room for a serial.
TEST_GCP = "9521234"


class Receipt:
    def __init__(self, job="job-1", event_ids=()):
        self.job = job
        self.event_ids = tuple(event_ids)

    @property
    def answerable(self):
        return bool(self.job)


class Outcome:
    def __init__(self, running=False, success=True, errors=(), known=True):
        self.running = running
        self.success = success
        self.known = known
        self.errors = tuple(errors)

    @property
    def settled(self):
        return not self.running


class FakeCapture:
    """Takes documents and remembers them; answers about jobs on request."""

    def __init__(self):
        self.documents = []
        self.outcome_to_give = Outcome()
        self.error_to_raise = None

    def submit(self, epcis_document):
        if self.error_to_raise:
            raise self.error_to_raise
        self.documents.append(epcis_document)
        return Receipt(job="job-%d" % len(self.documents))

    def outcome(self, job):
        return self.outcome_to_give

    def check(self):
        return ""

    # -- convenience for assertions -------------------------------------

    @property
    def events(self):
        return [
            event
            for document in self.documents
            for event in document["epcisBody"]["eventList"]
        ]


class FakeQuery:
    """Hands back events that were put into it, and remembers what it was asked."""

    def __init__(self):
        self.pages = []
        self.asked = []
        self.error_to_raise = None

    def since(self, since="", per_page=100, pages=50):
        self.asked.append(since)
        if self.error_to_raise:
            raise self.error_to_raise
        return list(self.pages.pop(0)) if self.pages else []

    def for_epc(self, epc, per_page=100):
        return []

    def check(self):
        return ""


class EventCase(LotCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write(
            {
                "openepcis_events_enabled": True,
                "openepcis_epcis_url": "https://api.example.test",
                "openepcis_gcp": TEST_GCP,
            }
        )
        cls.warehouse = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.company.id)], limit=1
        )
        cls.warehouse.lot_stock_id.openepcis_gln = TEST_GLN
        cls.supplier_location = cls.env.ref("stock.stock_location_suppliers")
        cls.customer_location = cls.env.ref("stock.stock_location_customers")

    def setUp(self):
        super().setUp()
        self.capture = FakeCapture()
        patcher = patch.object(
            OpenepcisClient,
            "_epcis_capture",
            lambda _self, company=None: self.capture,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.query = FakeQuery()
        query_patcher = patch.object(
            OpenepcisClient,
            "_epcis_query",
            lambda _self, company=None: self.query,
        )
        query_patcher.start()
        self.addCleanup(query_patcher.stop)

    # ------------------------------------------------------------------
    # Movements
    # ------------------------------------------------------------------

    def _transfer(self, picking_type, product, quantity=3, lot_name=None, partner=None):
        """A validated transfer of one product, with or without a lot."""
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": picking_type.default_location_src_id.id
                or self.supplier_location.id,
                "location_dest_id": picking_type.default_location_dest_id.id
                or self.customer_location.id,
                "partner_id": partner and partner.id,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": product.name,
                            "product_id": product.id,
                            "product_uom_qty": quantity,
                            "product_uom": product.uom_id.id,
                        },
                    )
                ],
            }
        )
        picking.action_confirm()
        picking.action_assign()
        for line in picking.move_line_ids:
            line.quantity = quantity
            if lot_name:
                line.lot_name = lot_name
        picking.move_ids.picked = True
        picking.button_validate()
        return picking

    def _open_transfer(self, picking_type, product, quantity=3, lot=None, partner=None):
        """A transfer that is reserved and waiting — the only kind an event may advance."""
        picking = self.env["stock.picking"].create(
            {
                "picking_type_id": picking_type.id,
                "location_id": picking_type.default_location_src_id.id
                or self.supplier_location.id,
                "location_dest_id": picking_type.default_location_dest_id.id
                or self.customer_location.id,
                "partner_id": partner and partner.id,
                "move_ids": [
                    (
                        0,
                        0,
                        {
                            "name": product.name,
                            "product_id": product.id,
                            "product_uom_qty": quantity,
                            "product_uom": product.uom_id.id,
                        },
                    )
                ],
            }
        )
        picking.action_confirm()
        picking.action_assign()
        for line in picking.move_line_ids:
            line.quantity = quantity
            if lot is not None:
                line.lot_id = lot.id
        picking.move_ids.picked = True
        return picking

    def _incoming_type(self):
        picking_type = self.warehouse.in_type_id
        picking_type.use_create_lots = True
        picking_type.use_existing_lots = True
        return picking_type

    def _outgoing_type(self):
        picking_type = self.warehouse.out_type_id
        picking_type.use_existing_lots = True
        return picking_type

    def _queued(self):
        return self.env["openepcis.event"].search([])

    def _deliver(self):
        self.env["openepcis.event"]._cron_capture()


__all__ = [
    "TEST_GTIN",
    "TEST_GLN",
    "TEST_GLN_DOCK",
    "TEST_PARTNER_GLN",
    "TEST_GCP",
    "EventCase",
    "FakeCapture",
    "Outcome",
]
