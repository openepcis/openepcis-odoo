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
    idempotency_key,
    instance_uri,
    object_event,
    party,
    quantity_element,
    transaction_event,
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
            event_time = picking._openepcis_event_time()
            if not event_time:
                picking.message_post(
                    body=_(
                        "No EPCIS event was reported: this transfer carries no completion "
                        "time, and an event has to say when it happened. The moment of "
                        "reporting is not that moment — it would put a different identity "
                        "on the same movement every time it was sent."
                    )
                )
                continue
            picking._openepcis_queue_aggregations(read_point)
            picking._openepcis_queue_movement(read_point)
            picking._openepcis_queue_return_release(read_point)

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
        )
        self.env["openepcis.event"].queue(
            document([event]),
            self.name,
            self.company_id,
            idem_key=self._openepcis_idem_key(biz_step, epcs, quantities),
            source=self,
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
        """What this transfer did to logistic units: took them apart, filled them.

        Reported before the movement, in the order things happen: goods come
        out of a unit before they go into another, and the unit moves last.

        Both halves are needed, and for a while only one was here. An
        aggregation is a standing statement — scan the SSCC and the repository
        answers what is underneath it — so a unit that is emptied and never
        said so keeps answering with goods that are no longer in it. The
        operation types could already be labelled ``unpacking``, which made the
        gap easy to miss: the label was there, the event was not.
        """
        self.ensure_one()
        self._openepcis_queue_aggregation(
            read_point, self._openepcis_units_emptied(), cbv.DELETE, cbv.UNPACKING
        )
        self._openepcis_queue_aggregation(
            read_point, self._openepcis_units_filled(), cbv.ADD, cbv.PACKING
        )

    def _openepcis_units_emptied(self):
        """Units this transfer took goods out of, and the lines that left them."""
        return self._openepcis_by_package("package_id", "result_package_id")

    def _openepcis_units_filled(self):
        """Units this transfer put goods into, and the lines that went in."""
        return self._openepcis_by_package("result_package_id", "package_id")

    def _openepcis_by_package(self, field, other):
        """Lines grouped by the unit in ``field``, where that unit changed.

        Goods that stay in the unit they were already in were neither packed
        nor unpacked — a pallet that merely moves is a movement, and reporting
        it as a fresh packing would restate an aggregation that never changed.
        So a line only counts where the two package fields differ.

        A unit without an SSCC is skipped on both sides. We never told anybody
        it held anything, so there is nothing to add to and nothing to take
        apart.
        """
        self.ensure_one()
        grouped = {}
        for line in self._openepcis_lines():
            package = line[field]
            if package and package.openepcis_sscc and package != line[other]:
                grouped.setdefault(package, self.env["stock.move.line"])
                grouped[package] |= line
        return grouped

    def _openepcis_queue_aggregation(self, read_point, by_package, action, biz_step):
        self.ensure_one()
        for package, lines in by_package.items():
            epcs, quantities = self._openepcis_identifiers(lines)
            if not epcs and not quantities:
                continue
            event = aggregation_event(
                action=action,
                event_time=self._openepcis_event_time(),
                parent_id=package._openepcis_uri(),
                biz_step=biz_step,
                disposition=cbv.IN_PROGRESS,
                child_epcs=epcs,
                child_quantities=quantities,
                read_point=read_point,
                biz_location=read_point,
                biz_transactions=self._openepcis_biz_transactions(),
            )
            self.env["openepcis.event"].queue(
                document([event]),
                "%s / %s" % (self.name, package.name),
                self.company_id,
                # The unit belongs in the key. Two pallets of the same lot in
                # one transfer state the same contents, so without it the
                # second pallet's event met the first one's row in the outbox
                # and was silently dropped.
                idem_key=self._openepcis_idem_key(
                    biz_step, epcs, quantities, package.openepcis_sscc
                ),
                source=self,
            )

    # ------------------------------------------------------------------
    # Paperwork the goods no longer belong to
    # ------------------------------------------------------------------

    def _openepcis_queue_return_release(self, read_point):
        """Goods coming back stop belonging to the shipment they went out on.

        The one place a TransactionEvent says something the movement cannot.
        Every event this connector sends already names the paperwork it belongs
        to, so associating goods with an order needs no event of its own — it
        is on the ObjectEvent already, and repeating it as a TransactionEvent
        ADD would state the same fact twice.

        Ending an association is different. It is a standing statement, like an
        aggregation: until something withdraws it, a despatch advice answers
        with goods that have long since come back. A receipt alone does not
        withdraw it — it says the goods arrived somewhere, not that they left a
        shipment — and nobody downstream can work the second out from the
        first.

        Only a return, and only against the transaction the original transfer
        named. A return of something that named no paperwork releases nothing.
        """
        self.ensure_one()
        original = self._openepcis_returned_from()
        if not original:
            return
        released = original._openepcis_biz_transactions()
        if not released:
            return
        epcs, quantities = self._openepcis_identifiers(self._openepcis_lines())
        if not epcs and not quantities:
            return
        event = transaction_event(
            action=cbv.DELETE,
            event_time=self._openepcis_event_time(),
            biz_transactions=released,
            biz_step=cbv.RECEIVING,
            disposition=cbv.RETURNED,
            epcs=epcs,
            quantities=quantities,
            read_point=read_point,
            biz_location=read_point,
        )
        self.env["openepcis.event"].queue(
            document([event]),
            _("%(name)s (released from %(original)s)", name=self.name, original=original.name),
            self.company_id,
            idem_key=self._openepcis_idem_key("release", epcs, quantities, original.name),
            source=self,
        )

    def _openepcis_returned_from(self):
        """The transfer this one sends goods back on, if it is a return.

        ``return_id`` only exists from Odoo 17 on, and this addon reads it the
        way it reads ``purchase_id``: by asking whether the field is there,
        rather than by depending on a module for one attribute.
        """
        self.ensure_one()
        if "return_id" not in self._fields:
            return self.browse()
        return self.return_id

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
        """When it happened, said in UTC — or nothing at all.

        Odoo keeps datetimes naive and in UTC; EPCIS wants an instant and an
        offset. Marking it UTC rather than converting to the user's zone is the
        honest reading: the transfer was completed at that instant, and whose
        clock was on the wall is a separate question.

        Only ``date_done``. The two fallbacks that used to stand behind it are
        both gone, and for different reasons. ``scheduled_date`` is a planned
        time, not an observed one — reporting it as the moment of the movement
        is a false statement even when it happens to be close. And the clock at
        reporting time is worse: the event time goes into the canonical event
        hash, which is the event's identity, so a made-up time puts a different
        identity on every retry of the same movement.

        In practice ``date_done`` is always set here — the hook only runs for a
        transfer that reached ``done``, and Odoo stamps it in ``_action_done``.
        Returning nothing is the honest answer for the case that is left.
        """
        self.ensure_one()
        return self.date_done.replace(tzinfo=timezone.utc) if self.date_done else None

    def _openepcis_biz_transactions(self):
        """The paperwork this movement belongs to, as something followable.

        Incoming goods answer to the order that brought them, outgoing goods to
        the despatch advice that accompanies them. This is the join between the
        physical record and the commercial one — the reason a warehouse event
        can say which order it belonged to without the asker having an ERP
        login.

        The CBV form ``urn:epcglobal:cbv:bt:<GLN>:<number>``, not a URL. The
        schema takes either, and a URL is the more useful of the two — it can
        be followed. But the reference is part of the canonical event hash, and
        therefore part of the event's identity: a URL would carry this
        deployment's address into it, so the same movement reported from a test
        system and from production would have two different names. The GLN
        carries what the host would otherwise have said, namely who issued the
        document.
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
        """The number of the document this movement belongs to.

        The source document is the better answer where there is one — a receipt
        belongs to a purchase order, not to itself — and ``purchase_id`` only
        exists when Purchase is installed, which this addon does not require.

        The number, and deliberately not this database's URL for it, although a
        URL is the more useful of the two forms the schema allows: it can be
        followed, and its host says who issued the document. The reference goes
        into the canonical event hash, and the hash is the event's identity.
        With a URL in it, the identity would carry ``web.base.url`` — a move
        from a test system to production would re-mint every identifier ever
        issued, and the same movement would have two names depending on where
        it was reported from. The CBV form names the same document without
        saying where it is hosted; ``biz_transaction`` builds it from this
        number and the GLN of the party that issued it, because order number
        4711 is only unique alongside whoever wrote it.
        """
        self.ensure_one()
        record = self
        for field in ("purchase_id", "sale_id"):
            if field in self._fields and self[field]:
                record = self[field]
                break
        return record.display_name if record is not self else (fallback or self.name)

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

    def _openepcis_idem_key(self, biz_step, epcs, quantities, *extra):
        """This database's own handle on a movement it has already reported.

        The database, the transfer and the business step make it *this*
        movement; the identifiers make it this content. ``extra`` is for what
        those two do not separate — an aggregation passes the unit's SSCC,
        because two pallets of the same lot in one transfer state the same
        contents and would otherwise share a key. Nothing here changes
        between two reports of the same movement, which is the point: the
        second report finds the first row and adds nothing.

        This used to be the event's ``eventID``. It is not any more — the
        eventID is the canonical CBV hash, which the repository computes — and
        the two had to be separated for two reasons. An ErrorDeclaration
        repeats the eventID of the event it corrects, so the eventID cannot
        carry a uniqueness constraint; and this key deliberately contains the
        database UUID, which has no business being in an event's identity.
        """
        self.ensure_one()
        database = self.env["ir.config_parameter"].sudo().get_param("database.uuid") or "odoo"
        content = sorted(epcs) + sorted(element["epcClass"] for element in quantities)
        return idempotency_key(database, self.name, biz_step, *extra, *content)
