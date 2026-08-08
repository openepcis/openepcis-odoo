# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Units of measure, in the code GS1 speaks.

Odoo names units for people ("kg", "Units"); the catalog wants UN/CEFACT
Recommendation 20 codes ("KGM", "H87"). One field bridges the two, and a
post-install pass fills in the units Odoo ships so that nobody has to look up
twenty codes before the first product can be published.
"""

from odoo import fields, models

#: XML id of an Odoo unit -> its UN/CEFACT Rec 20 common code.
#: Resolved leniently: Odoo has reorganised its unit records before, and a unit
#: this release does not have is simply one this table skips.
REC20_BY_XMLID = {
    "uom.product_uom_unit": "H87",  # piece
    "uom.product_uom_dozen": "DZN",
    "uom.product_uom_kgm": "KGM",
    "uom.product_uom_gram": "GRM",
    "uom.product_uom_ton": "TNE",
    "uom.product_uom_lb": "LBR",
    "uom.product_uom_oz": "ONZ",
    "uom.product_uom_litre": "LTR",
    "uom.product_uom_cubic_meter": "MTQ",
    "uom.product_uom_gal": "GLL",
    "uom.product_uom_qt": "QT",
    "uom.product_uom_floz": "OZA",
    "uom.product_uom_meter": "MTR",
    "uom.product_uom_km": "KMT",
    "uom.product_uom_cm": "CMT",
    "uom.product_uom_mm": "MMT",
    # Odoo 19 renamed some of these and added others. Both spellings are listed
    # so one table serves both releases; a name the running release does not have
    # is simply skipped.
    "uom.product_uom_millimeter": "MMT",
    "uom.product_uom_milliliter": "MLT",
    "uom.product_uom_minute": "MIN",
    "uom.product_uom_kwh": "KWH",
    "uom.product_uom_mile": "SMI",
    "uom.product_uom_foot": "FOT",
    "uom.product_uom_inch": "INH",
    "uom.product_uom_yard": "YRD",
    "uom.product_uom_square_meter": "MTK",
    "uom.product_uom_square_foot": "FTK",
    "uom.product_uom_day": "DAY",
    "uom.product_uom_hour": "HUR",
    # Deliberately absent: uom.product_uom_pack_6. "Pack of 6" is a packaging
    # count, not a UN/CEFACT measure, and guessing a code for it would put a
    # wrong unit on a published measurement. A record that uses it is reported
    # as incomplete instead — see OpenepcisFieldMapping._has_value.
}


class UomUom(models.Model):
    _inherit = "uom.uom"

    openepcis_rec20_code = fields.Char(
        string="UN/CEFACT code",
        size=3,
        help="UN/CEFACT Recommendation 20 common code, e.g. KGM for kilogram, "
        "LTR for litre, H87 for piece. Sent with every measurement.",
    )

    def _openepcis_fill_rec20_codes(self):
        """Fill in the codes for Odoo's own units, leaving edited ones alone."""
        for xml_id, code in REC20_BY_XMLID.items():
            unit = self.env.ref(xml_id, raise_if_not_found=False)
            if unit and not unit.openepcis_rec20_code:
                unit.openepcis_rec20_code = code
