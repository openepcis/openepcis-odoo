# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""A till is not a loading bay, although stock cannot tell them apart.

Point of Sale moves its goods through an ordinary outgoing operation. Nothing
on that operation type says it belongs to a shop — not its code, not its
sequence — so the events addon would report a sale as a shipment, which is a
true statement about the stock and a wrong one about the goods: nothing is in
transit, somebody bought it and walked out.

The only place the difference is written down is the point-of-sale
configuration that points at the operation type. This looks there.
"""

from odoo import models
from odoo.addons.openepcis_connector_events.vendored import cbv


class StockPickingType(models.Model):
    _inherit = "stock.picking.type"

    def _openepcis_default_mapping(self):
        self.ensure_one()
        if self._openepcis_is_point_of_sale():
            return (cbv.RETAIL_SELLING, cbv.RETAIL_SOLD)
        return super()._openepcis_default_mapping()

    def _openepcis_reseed_for_till(self):
        """Give an operation type the meaning it has now, unless somebody chose one.

        The seeding recomputes on the operation's own codes, and becoming a
        till changes none of them — so without this the override next door
        would be correct and never reached.

        "Unless somebody chose one" is decided by looking: a value that is
        exactly what the seed would have written is not a choice, it is the
        seed. Anything else is left alone, because a person who set
        ``inspecting`` on a till meant it.
        """
        for picking_type in self:
            if not picking_type.id:
                continue
            wanted = picking_type._openepcis_default_mapping()
            current = (picking_type.openepcis_biz_step, picking_type.openepcis_disposition)
            if current == wanted:
                continue
            seeded_before = current in (
                super(StockPickingType, picking_type)._openepcis_default_mapping(),
                (cbv.RETAIL_SELLING, cbv.RETAIL_SOLD),
            )
            if not seeded_before:
                continue
            picking_type.write(
                {"openepcis_biz_step": wanted[0], "openepcis_disposition": wanted[1]}
            )

    def _openepcis_is_point_of_sale(self):
        self.ensure_one()
        if not self.id:
            return False
        return bool(self.env["pos.config"].sudo().search_count([("picking_type_id", "=", self.id)]))
