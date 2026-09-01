# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
{
    "name": "OpenEPCIS Connector: Expiration Dates",
    "summary": "Map the expiry dates on lots to their GS1 terms",
    "description": """
Adds mapping rows that publish the expiration dates product_expiry keeps on
lots — expiration date, best-before date — into the per-instance catalog
documents. Data only: the fields belong to product_expiry, the publishing to
openepcis_connector_stock, and this bridge merely connects them so a database
without product_expiry never carries mapping rows pointing at fields it does
not have.

Installed automatically when both are present.
""",
    "author": "benelog GmbH & Co. KG",
    "website": "https://openepcis.io",
    "category": "Inventory/Inventory",
    "version": "19.0.1.0.0",
    "license": "LGPL-3",
    "depends": [
        "openepcis_connector_stock",
        "product_expiry",
    ],
    "auto_install": True,
    "data": [
        "data/openepcis_expiry_mapping_data.xml",
    ],
    "installable": True,
    "application": False,
}
