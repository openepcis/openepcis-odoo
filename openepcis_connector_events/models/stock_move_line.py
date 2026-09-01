# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Reading move lines as EPCIS identifiers.

Every event this connector builds starts here: a set of move lines, read as
what they say about goods. A transfer reads its own lines this way, a
manufacturing order reads its components and its output the same way, and the
reading has to be identical in both — the same lot must come out as the same
``epcClass`` whether it arrived on a lorry or off a production line.

So the reading lives on the lines rather than on whoever happens to hold them.
"""

from odoo import models

from ..vendored import instance_uri, quantity_element

#: UN/CEFACT code for "piece". EPCIS reads a quantity without a unit as a
#: count of items, so stating it is not more precise, only more typing.
PIECE = "H87"


class StockMoveLine(models.Model):
    _inherit = "stock.move.line"

    def _openepcis_identifiers(self):
        """These lines, as EPCs and quantity elements.

        A serial is an instance and belongs in the EPC list; a lot is a class
        and belongs in the quantity list. Putting an LGTIN in an EPC list is
        the single most common way to build an event that validates and means
        the wrong thing.

        Quantity elements are merged per class: two lines of the same lot are
        one statement about that lot, not two. Goods whose product is not
        published are left out entirely — an identifier that resolves nowhere
        is not worth an event.
        """
        epcs = []
        merged = {}
        for line in self:
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

    def _openepcis_done(self):
        """The lines that actually moved something."""
        return self.filtered(lambda line: line.state == "done" and line.quantity > 0)
