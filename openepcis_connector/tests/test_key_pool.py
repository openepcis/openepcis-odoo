# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Drawing, confirming and handing back GS1 identifiers.

The property worth defending: a number is registered with GS1 *only* after the
record using it exists. Registration is irreversible, so getting the order wrong
burns numbers out of a company's prefix for good.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.openepcis_client import OpenepcisClient
from ..utils.exceptions import OpenepcisError
from .common import TEST_GTIN, OpenepcisCase

DRAWN = "9520000000028"


@tagged("post_install", "-at_install")
class TestKeyPool(OpenepcisCase):
    def pool(self, error=None):
        """Patch the client with a pool that answers a draw and records calls."""
        calls = []

        def fake_request(_self, method, path, payload=None, params=None, **kw):
            calls.append({"method": method, "path": path, "payload": payload})
            if error is not None and "/gs1de/keys" in path:
                raise error
            if path == "/gs1de/keys/draw":
                return {"key": DRAWN, "ai": "GTIN", "state": "CANDIDATE"}
            return {}

        patcher = patch.object(OpenepcisClient, "request", fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    # -- drawing -------------------------------------------------------

    def test_drawing_fills_the_barcode_and_holds_the_number(self):
        self.pool()
        product = self._product(barcode=False)
        product.action_openepcis_draw_key()

        self.assertEqual(product.barcode, DRAWN)
        self.assertEqual(product.openepcis_key_state, "candidate")

    def test_drawing_asks_for_the_right_application_identifier(self):
        calls = self.pool()
        self._product(barcode=False).action_openepcis_draw_key()
        self.assertEqual(calls[0]["payload"], {"ai": "01"})

    def test_a_party_draws_under_417(self):
        calls = self.pool()
        partner = self.env["res.partner"].create({"name": "Acme", "is_company": True})
        partner.action_openepcis_draw_key()
        self.assertEqual(calls[0]["payload"], {"ai": "417"})

    def test_drawing_over_an_existing_identifier_is_refused(self):
        self.pool()
        product = self._product()  # already has TEST_GTIN
        with self.assertRaises(UserError):
            product.action_openepcis_draw_key()
        self.assertEqual(product.barcode, TEST_GTIN)

    def test_a_missing_licence_says_so_in_words(self):
        self.pool(error=OpenepcisError("No client", status=409))
        product = self._product(barcode=False)
        with self.assertRaises(UserError) as caught:
            product.action_openepcis_draw_key()
        self.assertIn("licence", str(caught.exception))

    # -- confirming ----------------------------------------------------

    def test_nothing_is_registered_before_the_record_is_published(self):
        calls = self.pool()
        product = self._product(barcode=False)
        product.action_openepcis_draw_key()

        self.assertNotIn(
            "/gs1de/keys/01/%s/confirm" % DRAWN,
            [call["path"] for call in calls],
            "drawing must not register — that step is irreversible",
        )

    def test_publishing_registers_the_held_number(self):
        calls = self.pool()
        product = self._product(barcode=False)
        product.action_openepcis_draw_key()
        product.openepcis_publish = True
        product._openepcis_sync()

        paths = [call["path"] for call in calls]
        # The catalog resource spells a GTIN with fourteen digits; the GS1.de
        # key registry below is a different API and keeps the drawn form.
        self.assertIn("/products/0%s" % DRAWN, paths)
        self.assertIn("/gs1de/keys/01/%s/confirm" % DRAWN, paths)
        # Order matters: the record must exist before GS1 is told about the key.
        self.assertLess(
            paths.index("/products/0%s" % DRAWN),
            paths.index("/gs1de/keys/01/%s/confirm" % DRAWN),
        )
        self.assertEqual(product.openepcis_key_state, "registered")

    def test_a_failed_registration_does_not_unpublish_the_record(self):
        calls = []

        def fake_request(_self, method, path, payload=None, params=None, **kw):
            calls.append(path)
            if path == "/gs1de/keys/draw":
                return {"key": DRAWN}
            if "confirm" in path:
                raise OpenepcisError("GS1 unreachable", status=504)
            return {}

        patcher = patch.object(OpenepcisClient, "request", fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)

        product = self._product(barcode=False)
        product.action_openepcis_draw_key()
        product.openepcis_publish = True
        product._openepcis_sync()

        # The record really is in the catalog; saying otherwise would be a lie.
        self.assertEqual(product.openepcis_state, "synced")
        self.assertEqual(product.openepcis_key_state, "candidate", "will be retried")

    # -- handing back --------------------------------------------------

    def test_a_candidate_can_be_handed_back(self):
        calls = self.pool()
        product = self._product(barcode=False)
        product.action_openepcis_draw_key()
        product.action_openepcis_release_key()

        self.assertFalse(product.barcode)
        self.assertEqual(product.openepcis_key_state, "own")
        self.assertIn("/gs1de/keys/01/%s" % DRAWN, [call["path"] for call in calls])

    def test_a_registered_number_cannot_be_handed_back(self):
        self.pool()
        product = self._product(barcode=False)
        product.action_openepcis_draw_key()
        product.openepcis_publish = True
        product._openepcis_sync()

        with self.assertRaises(UserError):
            product.action_openepcis_release_key()
        self.assertEqual(product.barcode, DRAWN)

    def test_an_own_number_cannot_be_handed_back_either(self):
        self.pool()
        product = self._product()
        with self.assertRaises(UserError):
            product.action_openepcis_release_key()
