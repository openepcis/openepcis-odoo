# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The first-load wizard.

The header row is pinned character for character. The resolver's CSV parser
looks columns up by name and ignores what it does not recognise, so a header
that is nearly right yields a run in which every row fails validation and
nothing says why.
"""

import csv
import io
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.openepcis_client import OpenepcisClient
from ..wizards.openepcis_bulk_import import ORGANIZATION_COLUMNS, PRODUCT_COLUMNS
from .common import TEST_GTIN, TEST_GTIN_2, OpenepcisCase


@tagged("post_install", "-at_install")
class TestBulkImport(OpenepcisCase):
    def upload(self, result=None):
        """Patch the multipart upload and keep what was sent."""
        uploads = []

        def fake_post_file(_self, path, filename, content, form=None, **kw):
            uploads.append({"path": path, "content": content, "form": form})
            return (
                result
                if result is not None
                else {
                    "total": 0,
                    "successCount": 0,
                    "errorCount": 0,
                    "errors": [],
                }
            )

        patcher = patch.object(OpenepcisClient, "post_file", fake_post_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        return uploads

    def _wizard(self, **values):
        return self.env["openepcis.bulk.import"].create(dict({"kind": "product"}, **values))

    @staticmethod
    def _parse(upload):
        return list(csv.DictReader(io.StringIO(upload["content"].decode("utf-8"))))

    # -- the format ----------------------------------------------------

    def test_the_header_is_exactly_what_the_parser_looks_for(self):
        uploads = self.upload()
        product = self._product()
        product.openepcis_publish = True
        self._wizard().action_import()

        header = upload_header = uploads[0]["content"].decode("utf-8").splitlines()[0].strip()
        self.assertEqual(header, ",".join(PRODUCT_COLUMNS))
        self.assertEqual(upload_header.split(",")[0], "gtin")

    def test_the_organization_header_matches_too(self):
        uploads = self.upload()
        partner = self.env["res.partner"].create(
            {"name": "Acme", "is_company": True, "openepcis_gln": TEST_GTIN}
        )
        partner.openepcis_publish = True
        self._wizard(kind="organization").action_import()

        header = uploads[0]["content"].decode("utf-8").splitlines()[0].strip()
        self.assertEqual(header, ",".join(ORGANIZATION_COLUMNS))

    def test_the_format_part_says_csv(self):
        uploads = self.upload()
        product = self._product()
        product.openepcis_publish = True
        self._wizard().action_import()
        self.assertEqual(uploads[0]["form"], {"format": "csv"})

    def test_it_posts_to_the_bulk_collection(self):
        uploads = self.upload()
        product = self._product()
        product.openepcis_publish = True
        self._wizard().action_import()
        self.assertEqual(uploads[0]["path"], "/bulk/products")

    def test_a_row_carries_the_fields_the_parser_reads(self):
        uploads = self.upload()
        product = self._product()
        product.openepcis_publish = True
        self._wizard().action_import()

        row = self._parse(uploads[0])[0]
        self.assertEqual(row["gtin"], TEST_GTIN)
        self.assertEqual(row["productName_en"], "Test product")
        self.assertEqual(row["gpcCategoryCode"], "10000045")
        self.assertEqual(row["brandName"], "Test brand")
        self.assertEqual(row["countryOfOriginCode"], "DE")

    # -- what goes in the run ------------------------------------------

    def test_marked_scope_leaves_unmarked_records_alone(self):
        uploads = self.upload()
        marked = self._product()
        marked.openepcis_publish = True
        self._product(barcode=TEST_GTIN_2)  # not marked

        self._wizard(scope="marked").action_import()
        self.assertEqual(len(self._parse(uploads[0])), 1)

    def test_all_scope_takes_everything_with_an_identifier(self):
        # Membership rather than a count: a real database — and Odoo's own demo
        # data — holds products with barcodes that this test did not create, and
        # taking them along is exactly what "every record" is supposed to mean.
        uploads = self.upload()
        self._product()
        self._product(barcode=TEST_GTIN_2)

        self._wizard(scope="all").action_import()

        sent = {row["gtin"] for row in self._parse(uploads[0])}
        self.assertIn(TEST_GTIN, sent)
        self.assertIn(TEST_GTIN_2, sent)

    def test_all_scope_is_wider_than_marked_scope(self):
        uploads = self.upload()
        marked = self._product()
        marked.openepcis_publish = True
        self._product(barcode=TEST_GTIN_2)

        self._wizard(scope="marked").action_import()
        self._wizard(scope="all").action_import()

        self.assertLess(len(self._parse(uploads[0])), len(self._parse(uploads[1])))

    def test_a_malformed_key_is_left_out_rather_than_sent(self):
        uploads = self.upload()
        good = self._product()
        bad = self._product(barcode="9520000000007")  # wrong check digit
        (good | bad).openepcis_publish = True

        wizard = self._wizard()
        wizard.action_import()

        self.assertEqual(len(self._parse(uploads[0])), 1)
        self.assertIn("not a valid GS1 key", wizard.result_summary)

    def test_nothing_to_send_says_so(self):
        self.upload()
        with self.assertRaises(UserError):
            self._wizard().action_import()

    # -- what comes back -----------------------------------------------

    def test_accepted_rows_are_marked_published(self):
        self.upload(result={"total": 1, "successCount": 1, "errorCount": 0, "errors": []})
        product = self._product()
        product.openepcis_publish = True
        self._wizard().action_import()
        self.assertEqual(product.openepcis_state, "synced")

    def test_a_duplicate_counts_as_already_published(self):
        # The catalog already holds the key — which is the state the wizard was
        # trying to reach. Calling that a failure would be pedantic and wrong.
        self.upload(
            result={
                "total": 1,
                "successCount": 0,
                "errorCount": 1,
                "errors": [
                    {"rowNumber": 1, "errorCode": "DUPLICATE_GTIN", "errorMessage": "exists"}
                ],
            }
        )
        product = self._product()
        product.openepcis_publish = True
        wizard = self._wizard()
        wizard.action_import()

        self.assertEqual(product.openepcis_state, "synced")
        self.assertIn("already in the catalog", wizard.result_summary)

    def test_a_real_row_failure_leaves_the_record_unpublished(self):
        self.upload(
            result={
                "total": 1,
                "successCount": 0,
                "errorCount": 1,
                "errors": [
                    {
                        "rowNumber": 1,
                        "errorCode": "VALIDATION_ERROR",
                        "errorMessage": "gpcCategoryCode is required",
                    }
                ],
            }
        )
        product = self._product()
        product.openepcis_publish = True
        wizard = self._wizard()
        wizard.action_import()

        self.assertNotEqual(product.openepcis_state, "synced")
        self.assertIn("gpcCategoryCode", wizard.error_detail)
