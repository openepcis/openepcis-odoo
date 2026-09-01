# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""A manufacturing order, reported as the one thing a movement cannot say.

Every other event this connector sends is about goods that continue to exist:
they arrive, they move, they are packed. A transformation is the exception —
components stop being what they were, output begins, and the claim worth making
is that the second came out of the first.

Nothing else carries that claim. Without it a chain of custody ends at the
factory door: the flour arrived, the bread left, and no query connects them. It
is also the one EPCIS event with no ``action``, because "these came into being"
and "these ceased to be" are both true at once and the field could only say one
of them.
"""

import logging
from datetime import timezone

from odoo import _, models
from odoo.addons.openepcis_connector_events.vendored import (
    document,
    idempotency_key,
    transformation_event,
)

logger = logging.getLogger(__name__)


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    def button_mark_done(self):
        """Report the finished order, after Odoo has finished with it.

        Reported afterwards on purpose: before the call the moves are not done
        and the lots of the output may not exist yet. And as everywhere in this
        connector, a report never breaks the operation — a plant does not stop
        producing because a repository is unreachable.
        """
        result = super().button_mark_done()
        for production in self:
            try:
                production._openepcis_report_transformation()
            except Exception:  # noqa: BLE001 — a report must never fail production
                logger.exception("OpenEPCIS: reporting %s failed", production.name)
        return result

    def _openepcis_report_transformation(self):
        self.ensure_one()
        if self.state != "done" or not self._openepcis_armed():
            return
        read_point = self._openepcis_read_point()
        if not read_point:
            self.message_post(
                body=_(
                    "No EPCIS event was reported: neither %(from_place)s nor %(to_place)s "
                    "carries a GLN, and an event has to say where it happened. Set one on "
                    "the warehouse location — sub-locations inherit it.",
                    from_place=self.location_src_id.display_name,
                    to_place=self.location_dest_id.display_name,
                )
            )
            return
        event_time = self._openepcis_event_time()
        if not event_time:
            self.message_post(
                body=_(
                    "No EPCIS event was reported: this order carries no completion time, "
                    "and an event has to say when it happened. The moment of reporting is "
                    "not that moment — it would put a different identity on the same "
                    "production run every time it was sent."
                )
            )
            return

        consumed = self.move_raw_ids.move_line_ids._openepcis_done()
        produced = self.move_finished_ids.move_line_ids._openepcis_done()
        input_epcs, input_quantities = consumed._openepcis_identifiers()
        output_epcs, output_quantities = produced._openepcis_identifiers()
        if not (input_epcs or input_quantities) or not (output_epcs or output_quantities):
            # Half a transformation is not a smaller statement, it is a
            # different and false one: "this came from nothing" or "this became
            # nothing". Where only one side is publishable, the honest answer
            # is to say nothing and let the movements speak for themselves.
            logger.info(
                "OpenEPCIS: %s has no publishable goods on one side — nothing reported",
                self.name,
            )
            return

        picking_type = self.picking_type_id
        event = transformation_event(
            event_time=event_time,
            input_epcs=input_epcs,
            input_quantities=input_quantities,
            output_epcs=output_epcs,
            output_quantities=output_quantities,
            biz_step=picking_type.openepcis_biz_step,
            disposition=picking_type.openepcis_disposition,
            read_point=read_point,
            biz_location=read_point,
        )
        self.env["openepcis.event"].queue(
            document([event]),
            self.name,
            self.company_id,
            idem_key=self._openepcis_idem_key(
                input_epcs, input_quantities, output_epcs, output_quantities
            ),
            source=self,
        )

    def _openepcis_armed(self):
        """The same two switches every other event answers to."""
        self.ensure_one()
        picking_type = self.picking_type_id
        return bool(
            self.env["openepcis.client"]._epcis_configured(self.company_id)
            and picking_type.openepcis_capture
            and picking_type.openepcis_biz_step
        )

    def _openepcis_event_time(self):
        """When the run finished, said in UTC — or nothing.

        ``date_finished`` and not the clock, for the reason the whole identity
        scheme rests on: the event time goes into the canonical hash, so an
        invented one would give the same production run a different name every
        time it was sent.
        """
        self.ensure_one()
        return self.date_finished.replace(tzinfo=timezone.utc) if self.date_finished else None

    def _openepcis_read_point(self):
        """Where the transformation happened.

        The finished-goods location first: that is where the output came into
        being, and it is the place a reader of the event would go looking. The
        component location stands in when it carries the GLN and the other does
        not.
        """
        self.ensure_one()
        for location in (self.location_dest_id, self.location_src_id):
            gln = location._openepcis_read_point() if location else ""
            if gln:
                return gln
        return ""

    def _openepcis_idem_key(self, input_epcs, input_quantities, output_epcs, output_quantities):
        """This database's handle on a production run it has already reported."""
        self.ensure_one()
        database = self.env["ir.config_parameter"].sudo().get_param("database.uuid") or "odoo"
        content = (
            sorted(input_epcs)
            + sorted(element["epcClass"] for element in input_quantities)
            + sorted(output_epcs)
            + sorted(element["epcClass"] for element in output_quantities)
        )
        return idempotency_key(database, self.name, "transformation", *content)
