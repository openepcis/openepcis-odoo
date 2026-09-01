# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
{
    "name": "OpenEPCIS Connector: Point of Sale events",
    "summary": "Recognise a till, so a sale reads as a sale",
    "description": """
A point-of-sale order leaves the warehouse through an ordinary outgoing
operation, and to the stock module it looks exactly like a delivery. It is not
one: the goods are not in transit to a customer's address, they have been sold
and carried out of the shop.

EPCIS has a word for that — retail_selling, leaving the goods retail_sold — and
this is the one thing no code on the operation type reveals. This bridge knows
where to look, and does nothing else.

Installed automatically when both the events addon and Point of Sale are
present.
""",
    "author": "benelog GmbH & Co. KG",
    "website": "https://openepcis.io",
    "category": "Inventory/Inventory",
    "version": "18.0.1.1.0",
    "license": "LGPL-3",
    "depends": [
        "openepcis_connector_events",
        "point_of_sale",
    ],
    "post_init_hook": "reseed_tills",
    "auto_install": True,
    "installable": True,
    "application": False,
}
