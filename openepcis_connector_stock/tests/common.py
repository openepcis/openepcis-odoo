# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Scaffolding on top of the main addon's: products that track, lots to publish.

The HTTP client stays fully stubbed, for the same reasons as in the main
addon's suite. Identifiers stay in the reserved 952 test range.
"""

from odoo.addons.openepcis_connector.tests.common import (
    TEST_GTIN,
    TEST_GTIN_2,
    TEST_GTIN_14,
    OpenepcisCase,
)


class LotCase(OpenepcisCase):
    def _tracked_product(self, tracking="lot", **values):
        """A publishable product whose template tracks lots or serials."""
        product = self._product(**values)
        product.product_tmpl_id.tracking = tracking
        return product

    def _published_product(self, tracking="lot", **values):
        """A product already in the catalog, seeded rather than synced.

        Written with the syncing guard so that seeding the state does not
        re-queue the record — the same idiom the main suite uses. Tests about
        the product-to-lot handover publish for real instead of calling this.
        """
        product = self._tracked_product(tracking=tracking, **values)
        product.with_context(openepcis_syncing=True).write(
            {"openepcis_publish": True, "openepcis_state": "synced"}
        )
        return product

    def _lot(self, name, product):
        return self.env["stock.lot"].create({"name": name, "product_id": product.id})


__all__ = ["TEST_GTIN", "TEST_GTIN_2", "TEST_GTIN_14", "LotCase"]
