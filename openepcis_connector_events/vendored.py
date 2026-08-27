# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""The vendored client library, reached from this addon.

It is vendored once, into openepcis_connector, and every bridge addon imports
it from there. A second copy would drift from the first, and the two would
disagree about a check digit on a Tuesday.
"""

from odoo.addons.openepcis_connector.vendor.benelog_client.core import gs1  # noqa: F401
from odoo.addons.openepcis_connector.vendor.benelog_client.core.client import Client  # noqa: F401
from odoo.addons.openepcis_connector.vendor.benelog_client.core.config import (  # noqa: F401
    ClientConfig,
)
from odoo.addons.openepcis_connector.vendor.benelog_client.core.errors import (  # noqa: F401
    BenelogError,
)
from odoo.addons.openepcis_connector.vendor.benelog_client.events import (  # noqa: F401
    Capture,
    Query,
    aggregation_event,
    biz_transaction,
    cbv,
    document,
    event_id,
    instance_uri,
    object_event,
    party,
    quantity_element,
    sscc_uri,
)
