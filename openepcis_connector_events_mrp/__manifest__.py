# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
{
    "name": "OpenEPCIS Connector: Manufacturing events",
    "summary": "What went in, what came out, and that the two belong together",
    "description": """
A movement says where goods are. A transformation says that some goods stopped
existing and others began, and that the second came from the first — and that
link is the only thing in EPCIS that carries a claim across a production step.
Without it a chain of custody ends at the factory door: the flour arrived, the
bread left, and nothing connects them.

Odoo knows both halves of a manufacturing order, the components consumed and
the goods produced, and this bridge reports them as the one TransformationEvent
they are.

Installed automatically when both the events addon and Manufacturing are
present.
""",
    "author": "benelog GmbH & Co. KG",
    "website": "https://openepcis.io",
    "category": "Manufacturing",
    "version": "19.0.1.0.0",
    "license": "LGPL-3",
    "depends": [
        "openepcis_connector_events",
        "mrp",
    ],
    "auto_install": True,
    "installable": True,
    "application": False,
}
