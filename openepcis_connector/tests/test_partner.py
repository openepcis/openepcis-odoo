# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Contacts published as GS1 organizations."""

from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import TEST_GLN, OpenepcisCase


@tagged("post_install", "-at_install")
class TestPartner(OpenepcisCase):
    def _company_partner(self, **values):
        defaults = {
            "name": "Acme Manufacturing",
            "is_company": True,
            "openepcis_gln": TEST_GLN,
            "street": "Hauptstraße 1",
            "city": "Köln",
            "zip": "50667",
            "country_id": self.env.ref("base.de").id,
            "email": "info@acme.test",
            "phone": "+49 221 000000",
        }
        defaults.update(values)
        return self.env["res.partner"].create(defaults)

    # -- the key -------------------------------------------------------

    def test_a_malformed_gln_is_refused_at_entry(self):
        with self.assertRaises(ValidationError):
            self._company_partner(openepcis_gln="9520000000007")  # wrong check digit

    def test_a_gln_of_the_wrong_length_is_refused(self):
        with self.assertRaises(ValidationError):
            self._company_partner(openepcis_gln="952000")

    def test_a_gln_identifies_one_party_only(self):
        self._company_partner()
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self._company_partner(name="Impostor Ltd").flush_recordset()

    # -- publishing ----------------------------------------------------

    def test_a_party_anchors_on_417_not_414(self):
        # 414 is a physical location, 417 is the party operating it. The
        # resolver routes them separately and has no /414 route for parties.
        self.capture()
        partner = self._company_partner()
        partner.openepcis_publish = True
        partner._openepcis_sync()
        self.assertEqual(
            partner.openepcis_digital_link, "https://id.example.test/417/%s" % TEST_GLN
        )

    def test_publishes_to_the_organizations_collection(self):
        calls = self.capture()
        partner = self._company_partner()
        partner.openepcis_publish = True
        partner._openepcis_sync()

        self.assertEqual(calls[0]["method"], "PUT")
        self.assertEqual(calls[0]["path"], "/organizations/%s" % TEST_GLN)
        self.assertEqual(calls[0]["payload"]["globalLocationNumber"], TEST_GLN)

    def test_an_individual_is_never_published(self):
        calls = self.capture()
        person = self._company_partner(name="Jane Doe", is_company=False)
        person.openepcis_publish = True
        person._openepcis_sync()

        self.assertEqual(calls, [], "a person is not an organization")
        self.assertEqual(person.openepcis_state, "error")

    # -- the address ---------------------------------------------------

    def test_address_parts_build_one_postal_address(self):
        calls = self.capture()
        partner = self._company_partner()
        partner.openepcis_publish = True
        partner._openepcis_sync()

        address = calls[0]["payload"]["address"]
        self.assertEqual(address["streetAddress"], {"en": "Hauptstraße 1"})
        self.assertEqual(address["addressLocality"], {"en": "Köln"})
        self.assertEqual(address["postalCode"], "50667")
        self.assertEqual(address["addressCountry"], {"countryCode": "DE"})

    def test_email_and_phone_share_one_contact_point(self):
        # Both map through contactPoint[], so they must land in the same object
        # rather than in two contact points that each know half the story.
        calls = self.capture()
        partner = self._company_partner()
        partner.openepcis_publish = True
        partner._openepcis_sync()

        contacts = calls[0]["payload"]["contactPoint"]
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["email"], "info@acme.test")
        self.assertEqual(contacts[0]["telephone"], "+49 221 000000")

    def test_the_organization_name_is_a_language_map(self):
        calls = self.capture()
        partner = self._company_partner()
        partner.openepcis_publish = True
        partner._openepcis_sync()
        self.assertEqual(calls[0]["payload"]["organizationName"], {"en": "Acme Manufacturing"})
