# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
{
    "name": "OpenEPCIS Connector",
    "summary": "Publish products and partners to a GS1 Digital Link resolver",
    "description": """
Publishes Odoo master data to an OpenEPCIS catalog behind a GS1-conformant
Digital Link resolver, and brings the resulting Digital Link and QR code back
onto the Odoo form.

- Products and partners are queued on save and published in the background.
- Field mapping is data, not code: adjust which Odoo field feeds which GS1 term.
- Identifiers can be drawn from your own GS1 company prefix pool.
- Shows what a downstream registry still requires before you publish.

Publication onward to GS1 national registries happens on the platform side;
this connector talks to the resolver's HTTP API only.
""",
    "author": "benelog GmbH & Co. KG",
    "website": "https://openepcis.io",
    "category": "Inventory/Inventory",
    "version": "18.0.1.0.0",
    "license": "LGPL-3",
    "depends": [
        "product",
    ],
    "data": [
        "security/ir.model.access.csv",
        "data/openepcis_field_mapping_data.xml",
        "data/openepcis_partner_mapping_data.xml",
        "data/ir_cron_data.xml",
        "views/openepcis_field_mapping_views.xml",
        "views/product_views.xml",
        "views/product_variant_views.xml",
        "views/res_partner_views.xml",
        "views/res_config_settings_views.xml",
        "wizards/openepcis_gpc_search_views.xml",
        "wizards/openepcis_bulk_import_views.xml",
        "report/openepcis_label_report.xml",
    ],
    "post_init_hook": "post_init_hook",
    "images": [
        "static/description/icon.png",
    ],
    "installable": True,
    "application": False,
}
