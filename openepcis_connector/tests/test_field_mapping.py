# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""What the mapping turns an Odoo product into.

The shapes asserted here are the catalog's, not this addon's: ``brand.brandName``
is a localised map inside an object, ``targetMarket`` is a list, a measurement is
``{value, unitCode}``. Getting one of them wrong earns an opaque 400 from a
registry, so they are pinned.
"""

from odoo.tests import tagged

from .common import TEST_GTIN, OpenepcisCase


@tagged("post_install", "-at_install")
class TestFieldMapping(OpenepcisCase):
    def _payload(self, product):
        return self.env["openepcis.field.mapping"].build_payload(product)

    def test_localized_text_becomes_a_language_map(self):
        payload = self._payload(self._product(name="Bicycle bell"))
        self.assertEqual(payload["productName"], {"en": "Bicycle bell"})

    def test_nested_term_is_placed_inside_its_object(self):
        payload = self._payload(self._product(openepcis_brand_name="Acme"))
        self.assertEqual(payload["brand"], {"brandName": {"en": "Acme"}})

    def test_list_term_becomes_a_single_element_list(self):
        payload = self._payload(self._product())
        self.assertEqual(
            payload["targetMarket"], [{"targetMarketCountries": {"countryCode": "DE"}}]
        )

    def test_country_is_sent_as_iso_alpha_2(self):
        # The platform's own code list says alpha-2 for gs1:countryCode, which
        # is exactly what res.country stores — no conversion table needed.
        payload = self._payload(self._product())
        self.assertEqual(payload["countryOfOrigin"], {"countryCode": "DE"})

    def test_measurement_takes_its_unit_from_the_named_field(self):
        payload = self._payload(self._product())
        self.assertEqual(payload["netContent"], {"value": 500.0, "unitCode": "H87"})

    def test_measurement_falls_back_to_the_fixed_unit(self):
        payload = self._payload(self._product(weight=2.5))
        self.assertEqual(payload["netWeight"], {"value": 2.5, "unitCode": "KGM"})

    def test_zero_measurement_is_omitted_rather_than_sent(self):
        # Odoo's weight defaults to 0.0, which means "not filled in". Sending it
        # would satisfy a registry's requirement with a falsehood.
        payload = self._payload(self._product(weight=0.0))
        self.assertNotIn("netWeight", payload)

    def test_empty_field_is_omitted(self):
        payload = self._payload(self._product(openepcis_brand_name=False))
        self.assertNotIn("brand", payload)

    def test_dotted_odoo_path_reads_across_a_relation(self):
        payload = self._payload(self._product())
        self.assertEqual(payload["gpcCategoryCode"], "10000045")

    def test_a_broken_row_does_not_cost_the_whole_payload(self):
        self.env["openepcis.field.mapping"].create(
            {
                "model_name": "product.product",
                "odoo_field": "no_such_field",
                "gs1_path": "functionalName",
                "value_type": "text",
            }
        )
        payload = self._payload(self._product())
        self.assertNotIn("functionalName", payload)
        self.assertIn("productName", payload)  # the rest survived

    def test_inactive_rows_are_ignored(self):
        self.env.ref("openepcis_connector.mapping_product_brand").active = False
        payload = self._payload(self._product())
        self.assertNotIn("brand", payload)

    def test_two_rows_sharing_a_parent_object_merge(self):
        self.env["openepcis.field.mapping"].create(
            {
                "model_name": "product.product",
                "odoo_field": "default_code",
                "gs1_path": "brand.subBrandName",
                "value_type": "localized",
            }
        )
        payload = self._payload(self._product(default_code="SUB-1"))
        self.assertEqual(
            payload["brand"],
            {"brandName": {"en": "Test brand"}, "subBrandName": {"en": "SUB-1"}},
        )

    def test_localized_covers_every_installed_language(self):
        self.env["res.lang"]._activate_lang("de_DE")
        product = self._product()
        product.with_context(lang="de_DE").name = "Fahrradklingel"
        payload = self._payload(product)
        self.assertEqual(payload["productName"]["de"], "Fahrradklingel")
        self.assertEqual(payload["productName"]["en"], "Test product")

    def test_key_is_added_by_the_mixin_not_the_mapping(self):
        product = self._product()
        self.assertNotIn("gtin", self._payload(product))
        self.assertEqual(product._openepcis_payload()["gtin"], TEST_GTIN)
