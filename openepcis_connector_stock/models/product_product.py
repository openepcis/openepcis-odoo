# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The product side of the ordering gate: release waiting lots when it lands."""

from odoo import models


class ProductProduct(models.Model):
    _inherit = "product.product"

    def _openepcis_record_success(self):
        """After the product reaches the catalog, queue the lots that waited for it.

        Error states are included on purpose: a lot parked after retries — or
        after a *Publish now* pressed before its product was published — is
        worth another attempt once the product demonstrably exists upstream.
        ``PUT`` is create-or-update, so the retry cannot produce a duplicate.
        """
        super()._openepcis_record_success()
        waiting = self.env["stock.lot"].search(
            [
                ("product_id", "in", self.ids),
                ("openepcis_publish", "=", True),
                ("openepcis_state", "in", ("not_synced", "error")),
            ]
        )
        if waiting:
            waiting._openepcis_mark_queued()
