# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Organizations changed in the catalog, read back into contacts."""

from unittest.mock import patch

from odoo.tests import tagged

from ..models.openepcis_client import OpenepcisClient
from ..vendor.openepcis_client.core.errors import OpenEpcisError
from ..vendor.openepcis_client.masterdata import Masterdata
from .common import TEST_GLN, OpenepcisCase

#: A second test-range GLN, check digit included.
OTHER_GLN = "9520000000011"


def organization(gln=TEST_GLN, name="Acme Manufacturing", city="Köln", **extra):
    document = {
        "globalLocationNumber": gln,
        "organizationName": {"de": name, "en": name},
        "address": {
            "streetAddress": {"de": "Hauptstraße 1"},
            "postalCode": "50667",
            "addressLocality": {"de": city},
            "addressCountry": {"countryCode": "DE"},
        },
        "contactPoint": [{"email": "info@acme.test", "telephone": "+49 221 000000"}],
    }
    document.update(extra)
    return document


class Catalog:
    """The resolver's organization list, as a stub the library walks."""

    def __init__(self, documents=None, error=None):
        self.documents = list(documents or [])
        self.error = error
        self.reads = 0

    def get(self, path, params=None):
        self.reads += 1
        if self.error:
            raise self.error
        return {"organizations": self.documents, "totalPages": 1 if self.documents else 0}


@tagged("post_install", "-at_install")
class TestPartnerPull(OpenepcisCase):
    def setUp(self):
        super().setUp()
        self.catalog = Catalog()
        patcher = patch.object(
            OpenepcisClient, "masterdata", lambda _self, company=None: Masterdata(self.catalog)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Partner = self.env["res.partner"]

    def pull(self):
        return self.Partner._openepcis_pull(self.company)

    def by_gln(self, gln=TEST_GLN):
        return self.Partner.search([("openepcis_gln", "=", gln)])

    def test_an_unknown_organization_becomes_a_published_company(self):
        self.catalog.documents = [organization()]
        counts = self.pull()
        self.assertEqual(counts["created"], 1)
        partner = self.by_gln()
        self.assertTrue(partner.is_company)
        self.assertEqual(partner.name, "Acme Manufacturing")
        self.assertEqual(partner.city, "Köln")
        self.assertEqual(partner.zip, "50667")
        self.assertEqual(partner.country_id, self.env.ref("base.de"))
        self.assertEqual(partner.email, "info@acme.test")
        # It came from the catalog, so it is published, and nothing is queued.
        self.assertTrue(partner.openepcis_publish)
        self.assertEqual(partner.openepcis_state, "synced")

    def test_a_change_in_the_catalog_reaches_the_contact_without_queuing_it(self):
        self.catalog.documents = [organization()]
        self.pull()
        self.catalog.documents = [organization(city="Hamm")]
        counts = self.pull()
        partner = self.by_gln()
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(partner.city, "Hamm")
        # Writing back what the catalog says must not send it out again.
        self.assertEqual(partner.openepcis_state, "synced")

    def test_an_unchanged_record_is_not_written_again(self):
        self.catalog.documents = [organization()]
        self.pull()
        partner = self.by_gln()
        partner.with_context(openepcis_syncing=True).write({"city": "Local edit"})
        counts = self.pull()
        self.assertEqual(counts["unchanged"], 1)
        self.assertEqual(partner.city, "Local edit")

    def test_a_contact_waiting_to_publish_keeps_its_local_edit(self):
        self.catalog.documents = [organization()]
        self.pull()
        partner = self.by_gln()
        partner.write({"city": "Bonn"})  # an ordinary edit queues it
        self.assertEqual(partner.openepcis_state, "queued")
        self.catalog.documents = [organization(city="Hamm")]
        counts = self.pull()
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(partner.city, "Bonn")

    def test_values_the_catalog_does_not_hold_are_left_alone(self):
        self.catalog.documents = [organization()]
        self.pull()
        partner = self.by_gln()
        partner.with_context(openepcis_syncing=True).write({"street2": "Hinterhaus"})
        document = organization(city="Hamm")
        document["contactPoint"] = []
        self.catalog.documents = [document]
        self.pull()
        self.assertEqual(partner.street2, "Hinterhaus")
        self.assertEqual(partner.email, "info@acme.test")

    def test_an_organization_without_a_usable_gln_or_name_is_skipped(self):
        broken_gln = organization(gln="9520000000007")  # wrong check digit
        nameless = organization(gln=OTHER_GLN)
        del nameless["organizationName"]
        self.catalog.documents = [broken_gln, nameless]
        counts = self.pull()
        self.assertEqual(counts["skipped"], 2)
        self.assertFalse(self.by_gln(OTHER_GLN))

    def test_an_unreachable_catalog_skips_the_company_without_raising(self):
        self.catalog.error = OpenEpcisError("down", status=503, path="/organizations")
        self.Partner._openepcis_cron_pull()
        self.assertGreater(self.catalog.reads, 0)
