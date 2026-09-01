# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
{
    "name": "OpenEPCIS Connector: Lots and Serial Numbers",
    "summary": "Publish lots and serial numbers as per-instance catalog documents",
    "description": """
Publishes Odoo's lots and serial numbers (stock.lot) to the OpenEPCIS catalog
as per-instance documents under the product's GTIN: a lot becomes
/01/<gtin>/10/<lot> (LGTIN, batch level), a serial number /01/<gtin>/21/<serial>
(SGTIN, single unit). The resulting instance-level Digital Link and its QR code
appear on the lot form.

A bridge module: the main connector depends on product only, and stock.lot
lives in stock. Installed automatically when both are present.
""",
    "author": "benelog GmbH & Co. KG",
    "website": "https://openepcis.io",
    "category": "Inventory/Inventory",
    "version": "19.0.1.0.0",
    "license": "LGPL-3",
    "depends": [
        "openepcis_connector",
        "stock",
    ],
    "auto_install": True,
    "data": [
        "data/openepcis_lot_mapping_data.xml",
        "data/ir_cron_data.xml",
        "views/stock_lot_views.xml",
    ],
    "installable": True,
    "application": False,
}
