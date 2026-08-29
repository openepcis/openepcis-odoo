# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
{
    "name": "OpenEPCIS Connector: Visibility Events",
    "summary": "Turn validated stock moves into EPCIS 2.0 visibility events",
    "description": """
Reports what happens to goods as EPCIS 2.0 events: a receipt, a transfer, a
pack, a delivery, a sale. Master data says what a product is; these say what
became of one of them, and together they are what a scanned serial number can
answer.

Every validated transfer becomes an ObjectEvent — what moved (SGTIN, LGTIN or
the trade item class), when, from which read point, and under which business
step. Packing into a logistic unit becomes an AggregationEvent under its SSCC,
so a pallet can say what is underneath it.

Events go to the EPCIS repository, which is a different service from the
resolver and behind a different permission. Nothing leaves the database until
the company is switched on and the operation type is armed, and the queue is an
outbox: validating a transfer never waits for the network.
""",
    "author": "benelog GmbH & Co. KG",
    "website": "https://openepcis.io",
    "category": "Inventory/Inventory",
    "version": "18.0.1.1.0",
    "license": "LGPL-3",
    "depends": [
        "openepcis_connector_stock",
    ],
    # The canonical CBV event hash. Used here for one thing — recognising an
    # event of ours when the inbox reads it back — and declared so Odoo refuses
    # to install the addon without it, rather than failing at the first
    # transfer. See openepcis_event.py on why the hash is compared and not sent.
    "external_dependencies": {"python": ["epcis_event_hash_generator"]},
    "data": [
        "security/ir.model.access.csv",
        "data/ir_sequence_data.xml",
        "data/ir_cron_data.xml",
        "views/openepcis_event_views.xml",
        "views/openepcis_inbound_event_views.xml",
        "views/res_partner_views.xml",
        "views/stock_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
}
