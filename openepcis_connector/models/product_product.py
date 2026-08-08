# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The product variant is the thing that gets published.

In GS1 terms a trade item is whatever a buyer can order and a scanner can
identify, and that is Odoo's ``product.product``: a shirt in three sizes is
three trade items with three GTINs. ``product.template`` has no GS1 counterpart,
which is why the mixin lives here and the template merely mirrors it.
"""

from odoo import _, api, models

from ..utils import gs1


class ProductProduct(models.Model):
    _name = "product.product"
    _inherit = ["product.product", "openepcis.sync.mixin", "openepcis.key.pool.mixin"]

    def _openepcis_key(self):
        self.ensure_one()
        return gs1.clean(self.barcode)

    def _openepcis_key_type(self):
        return "GTIN"

    def _openepcis_kind(self):
        return "PRODUCT"

    def _openepcis_key_field(self):
        return "barcode"

    def _openepcis_draw_ai(self):
        return gs1.ANCHOR_AI["GTIN"]

    def _openepcis_anchor_ai(self):
        return gs1.ANCHOR_AI["GTIN"]

    def _openepcis_endpoint(self):
        return "/products"

    def _openepcis_key_term(self):
        return "gtin"

    def _openepcis_check_ready(self):
        """The GS1 check, plus the one thing the catalog will not accept without."""
        blocker = super()._openepcis_check_ready()
        if blocker:
            return blocker
        # productName is the catalog's only hard requirement on a product, and
        # finding out through a 400 is a poor way to learn it.
        if not self.display_name:
            return _("The product has no name.")
        return ""

    @api.model
    def _openepcis_cron_sync(self):
        """Scheduled action entry point, kept here so the cron names a real model."""
        return super()._openepcis_cron_sync()
