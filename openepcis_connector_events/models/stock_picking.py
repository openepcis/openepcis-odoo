# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""A validated transfer, read as an EPCIS event.

An EPCIS event answers four questions about one moment, and a stock move line
already holds all four: *what* moved (the lot or serial on the line), *when*
(the transfer's completion), *where* (its locations) and *why* (the operation
type). This module does the reading, and nothing else: it queues, it does not
send. Validating a transfer must not wait for a repository, because the person
pressing Validate can do nothing about one that is down.

Three things it is careful about.

**A lot is not a serial number.** A serial identifies one unit and belongs in
``epcList``; a lot identifies a class of goods and belongs in ``quantityList``.
Untracked goods are a class too — the trade item itself — and are reported the
same way, which is why a warehouse that tracks nothing still produces useful
events.

**A read point is not optional here**, although EPCIS allows it to be. An event
that says something happened without saying where is half an answer, and half
answers are worse than a missing one because they look complete. A transfer
whose locations carry no GLN is reported in the chatter instead, where somebody
can see it and fix the location.

**Only published products appear.** An identifier that resolves nowhere is not
worth putting into a repository: a scan of it would find an event and no
product. What is not published is left out, and the event still describes what
did move.
"""

import logging
from datetime import timezone

from odoo import _, fields, models

from ..vendored import (
    aggregation_event,
    biz_transaction,
    cbv,
    document,
    event_id,
    instance_uri,
    object_event,
    party,
    quantity_element,
)

logger = logging.getLogger(__name__)

#: UN/CEFACT code for "piece". EPCIS reads a quantity without a unit as a count
#: of items, so sending H87 beside it says the same thing twice.
PIECE = "H87"


class StockPicking(models.Model):
    _inherit = "stock.picking"

    openepcis_event_ids = fields.One2many(
        "openepcis.event",
        compute="_compute_openepcis_event_ids",
        string="EPCIS events",
    )
    openepcis_event_count = fields.Integer(compute="_compute_openepcis_event_ids")

    def _compute_openepcis_event_ids(self):
        events = self.env["openepcis.event"]
        for picking in self:
            found = events.sudo().search(
                [("res_model", "=", "stock.picking"), ("res_id", "=", picking.id)]
            )
            picking.openepcis_event_ids = found
            picking.openepcis_event_count = len(found)

    # ------------------------------------------------------------------
    # The hook
    # ------------------------------------------------------------------

    def _action_done(self):
        result = super()._action_done()
        try:
            self.filtered(lambda picking: picking.state == "done")._openepcis_report()
        except Exception:  # noqa: BLE001 — a report must never fail a transfer
            logger.exception("OpenEPCIS: reporting %s failed", self.mapped("name"))
        return result

    def _openepcis_report(self):
        """Queue what this transfer says, one document per event."""
        for picking in self:
            if not picking._openepcis_armed():
                continue
            read_point = picking._openepcis_read_point()
            if not read_point:
                picking.message_post(
                    body=_(
                        "No EPCIS event was reported: neither %(from_place)s nor "
                        "%(to_place)s carries a GLN, and an event has to say where it "
                        "happened. Set one on the warehouse location — sub-locations "
                        "inherit it.",
                        from_place=picking.location_id.display_name,
                        to_place=picking.location_dest_id.display_name,
                    )
                )
                continue
            picking._openepcis_queue_aggregations(read_point)
            picking._openepcis_queue_movement(read_point)

    def _openepcis_armed(self):
        self.ensure_one()
        return bool(
            self.env["openepcis.client"]._epcis_configured(self.company_id)
            and self.picking_type_id.openepcis_capture
            and self.picking_type_id.openepcis_biz_step
        )

    # ------------------------------------------------------------------
    # The movement itself
    # ------------------------------------------------------------------

    def _openepcis_queue_movement(self, read_point):
        self.ensure_one()
        epcs, quantities = self._openepcis_identifiers(self._openepcis_lines())
        if not epcs and not quantities:
            return
        biz_step = self.picking_type_id.openepcis_biz_step
        event = object_event(
            action=cbv.OBSERVE,
            event_time=self._openepcis_event_time(),
            biz_step=biz_step,
            disposition=self.picking_type_id.openepcis_disposition,
            epcs=epcs,
            quantities=quantities,
            read_point=read_point,
            biz_location=read_point,
            biz_transactions=self._openepcis_biz_transactions(),
            source_list=self._openepcis_source_list(),
            destination_list=self._openepcis_destination_list(),
            event_identifier=self._openepcis_event_id(biz_step, epcs, quantities),
        )
        self.env["openepcis.event"].queue(
            document([event]), self.name, self.company_id, source=self
        )

    def _openepcis_lines(self):
        self.ensure_one()
        return self.move_line_ids.filtered(lambda line: line.state == "done" and line.quantity > 0)

    def _openepcis_identifiers(self, lines):
        """The lines, read as EPCs and quantity elements.

        Quantity elements are merged per class: two lines of the same lot are
        one statement about that lot, not two.
        """
        epcs = []
        merged = {}
        for line in lines:
            product = line.product_id
            gtin = product._openepcis_key()
            if not gtin or not product.openepcis_publish:
                continue
            lot_name = line.lot_id.name or line.lot_name
            if product.tracking == "serial" and lot_name:
                epcs.append(instance_uri(gtin, serial=lot_name))
                continue
            epc_class = instance_uri(gtin, lot=lot_name) if lot_name else instance_uri(gtin)
            uom = line.product_uom_id.openepcis_rec20_code or ""
            key = (epc_class, uom)
            merged[key] = merged.get(key, 0) + line.quantity
        quantities = [
            quantity_element(epc_class, quantity, uom if uom and uom != PIECE else None)
            for (epc_class, uom), quantity in merged.items()
        ]
        return epcs, quantities

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _openepcis_queue_aggregations(self, read_point):
        """One event per logistic unit this transfer packed.

        Reported before the movement, because that is the order they happened
        in: goods are put into a unit and the unit then moves.
        """
        self.ensure_one()
        by_package = {}
        for line in self._openepcis_lines():
            package = line.result_package_id
            if package and package.openepcis_sscc:
                by_package.setdefault(package, self.env["stock.move.line"])
                by_package[package] |= line
        for package, lines in by_package.items():
            epcs, quantities = self._openepcis_identifiers(lines)
            if not epcs and not quantities:
                continue
            event = aggregation_event(
                action=cbv.ADD,
                event_time=self._openepcis_event_time(),
                parent_id=package._openepcis_uri(),
                biz_step=cbv.PACKING,
                disposition=cbv.IN_PROGRESS,
                child_epcs=epcs,
                child_quantities=quantities,
                read_point=read_point,
                biz_location=read_point,
                biz_transactions=self._openepcis_biz_transactions(),
                event_identifier=self._openepcis_event_id(cbv.PACKING, epcs, quantities),
            )
            self.env["openepcis.event"].queue(
                document([event]),
                "%s / %s" % (self.name, package.name),
                self.company_id,
                source=self,
            )

    # ------------------------------------------------------------------
    # Where, when, under which paperwork
    # ------------------------------------------------------------------

    def _openepcis_read_point(self):
        """The GLN of the side of this transfer that is ours.

        Goods arriving are observed where they arrive; goods leaving, where
        they leave. For an internal move both sides are ours and the
        destination is the more useful answer — it is where the goods are now.
        """
        self.ensure_one()
        if self.picking_type_id.code == "outgoing":
            candidates = (self.location_id, self.location_dest_id)
        else:
            candidates = (self.location_dest_id, self.location_id)
        for location in candidates:
            gln = location and location._openepcis_read_point()
            if gln:
                return gln
        return ""

    def _openepcis_event_time(self):
        """When it happened, said in UTC.

        Odoo keeps datetimes naive and in UTC; EPCIS wants an instant and an
        offset. Marking it UTC rather than converting to the user's zone is the
        honest reading: the transfer was completed at that instant, and whose
        clock was on the wall is a separate question.
        """
        self.ensure_one()
        moment = self.date_done or self.scheduled_date or fields.Datetime.now()
        return moment.replace(tzinfo=timezone.utc)

    def _openepcis_biz_transactions(self):
        """The paperwork this movement belongs to, as something followable.

        Incoming goods answer to the order that brought them, outgoing goods to
        the despatch advice that accompanies them. This is the join between the
        physical record and the commercial one — the reason a warehouse event
        can say which order it belonged to without the asker having an ERP
        login.

        A URL, not a document number: the schema demands a URI either way, and
        of the two forms that satisfy it only one leads anywhere. The host also
        states who issued the document, which the alternative has to encode as a
        GLN to achieve. Where this database has no address of its own — the
        default ``localhost`` — the GLN form is used instead, because a link to
        somebody's laptop identifies nothing.
        """
        self.ensure_one()
        code = self.picking_type_id.code
        if code == "incoming":
            reference = self._openepcis_document_reference(self.origin)
            return [biz_transaction(cbv.PO, reference, self._openepcis_read_point())]
        if code == "outgoing":
            reference = self._openepcis_document_reference(self.name)
            return [biz_transaction(cbv.DESADV, reference, self._openepcis_read_point())]
        return []

    def _openepcis_document_reference(self, fallback):
        """This transfer's own address, or its number when it has none.

        The source document is the better answer where there is one — a receipt
        belongs to a purchase order, not to itself — and `purchase_id` only
        exists when Purchase is installed, which this addon does not require.
        """
        self.ensure_one()
        record = self
        for field in ("purchase_id", "sale_id"):
            if field in self._fields and self[field]:
                record = self[field]
                break
        base = (record.get_base_url() or "").rstrip("/")
        if not base or "localhost" in base or "127.0.0.1" in base:
            return fallback or self.name
        return "%s/odoo/%s/%s" % (base, record._name, record.id)

    def _openepcis_source_list(self):
        self.ensure_one()
        gln = self._openepcis_partner_gln()
        if gln and self.picking_type_id.code == "incoming":
            return [(cbv.OWNING_PARTY, party(gln))]
        return []

    def _openepcis_destination_list(self):
        self.ensure_one()
        gln = self._openepcis_partner_gln()
        if gln and self.picking_type_id.code == "outgoing":
            return [(cbv.OWNING_PARTY, party(gln))]
        return []

    def _openepcis_partner_gln(self):
        self.ensure_one()
        partner = self.partner_id
        return partner and partner.openepcis_gln or ""

    def _openepcis_event_id(self, biz_step, epcs, quantities):
        """An identifier derived from what this event states.

        The database, the transfer and the business step make it *this* event;
        the identifiers make it this content. Nothing here changes between two
        reports of the same movement, which is the point: the second report
        carries the identifier of the first.
        """
        self.ensure_one()
        database = self.env["ir.config_parameter"].sudo().get_param("database.uuid") or "odoo"
        content = sorted(epcs) + sorted(element["epcClass"] for element in quantities)
        return event_id(database, self.name, biz_step, *content)
