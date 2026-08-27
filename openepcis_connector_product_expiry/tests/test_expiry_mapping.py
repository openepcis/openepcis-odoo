# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The expiry dates travel into the instance document — and only the safe ones."""

from odoo.addons.openepcis_connector_stock.tests.common import LotCase
from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestExpiryMapping(LotCase):
    def test_expiry_dates_reach_the_instance_document(self):
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._published_product())
        # Two writes on purpose: product_expiry recomputes the other dates
        # when the expiration date changes, and the explicit best-before must
        # land after that recomputation to stick.
        lot.expiration_date = "2027-03-01 12:00:00"
        lot.write({"use_date": "2027-02-01 12:00:00", "openepcis_publish": True})
        lot._openepcis_sync()

        payload = calls[0]["payload"]
        self.assertEqual(payload["expirationDate"], "2027-03-01")
        self.assertEqual(payload["bestBeforeDate"], "2027-02-01")

    def test_the_removal_date_is_not_published_by_default(self):
        # The shipped row is inactive: equating Odoo's FEFO removal date with
        # GS1's last-day-at-retail is the administrator's call, not ours.
        calls = self.capture()
        lot = self._lot("BATCH-952-A", self._published_product())
        lot.write({"removal_date": "2027-01-15 12:00:00", "openepcis_publish": True})
        lot._openepcis_sync()

        self.assertNotIn("sellByDate", calls[0]["payload"])
