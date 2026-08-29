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

    def _openepcis_is_point_of_sale(self):
        self.ensure_one()
        if not self.id:
            return False
        return bool(self.env["pos.config"].sudo().search_count([("picking_type_id", "=", self.id)]))
