# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Where the events go, and whether they go at all.

The EPCIS repository is deliberately a second address rather than a path under
the resolver. They are two services with two jobs: the resolver answers *what
is this identifier*, the repository *what happened to it*. A deployment may
well run them on one host, but a connector that assumes so cannot be pointed at
one that does not.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..utils import sscc


class ResCompany(models.Model):
    _inherit = "res.company"

    openepcis_events_enabled = fields.Boolean(
        string="Report visibility events",
        help="Send validated transfers to the EPCIS repository as events. Off "
        "until the repository address is deposited and an operation type is armed.",
    )
    openepcis_epcis_url = fields.Char(
        string="EPCIS repository",
        help="Origin of the EPCIS 2.0 repository, e.g. https://api.epcis.cloud. "
        "A different service from the resolver, and usually a different host.",
    )
    openepcis_gcp = fields.Char(
        string="GS1 company prefix",
        help="Used to mint SSCCs for logistic units. Unlike a GTIN, an SSCC is "
        "not drawn from a registry: a company allocates its own from its prefix, "
        "and only has to make sure it never repeats one.",
    )

    openepcis_inbound_scope = fields.Selection(
        [
            ("all", "Everything the repository shows us"),
            ("own_gcp", "Only identifiers issued under our own prefix"),
            ("known", "Only what we can place in this database"),
        ],
        string="Which events to keep",
        default="all",
        required=True,
        help="A repository shared with partners hands out everything the "
        "credential may see, which in a busy chain is mostly other people's "
        "business. Narrowing costs nothing to change later: the watermark is "
        "the same either way, so a wider setting simply starts filling the "
        "inbox from the next run onwards.",
    )
    openepcis_inbound_batch = fields.Integer(
        string="Events per run",
        default=200,
        help="How many events one scheduled run asks for at a time.",
    )
    openepcis_inbound_pages = fields.Integer(
        string="Pages per run",
        default=20,
        help="How far one run will follow the repository's paging. This is the "
        "brake on catching up: a run that has been down for a day would "
        "otherwise walk a day of events in one transaction. What it does not "
        "reach stays behind the watermark and is fetched next run.",
    )

    openepcis_events_since = fields.Char(
        string="Events read up to",
        readonly=True,
        copy=False,
        help="How far the inbox has read, as a record time. The repository's own "
        "write clock rather than the reporter's, so it only moves forward and an "
        "outage costs one run rather than a reconciliation.",
    )

    @api.constrains("openepcis_gcp")
    def _check_openepcis_gcp(self):
        for company in self:
            if not company.openepcis_gcp:
                continue
            problem = sscc.problem_with_prefix(company.openepcis_gcp)
            if problem:
                raise ValidationError(
                    _(
                        "'%(prefix)s' cannot be a GS1 company prefix. %(why)s",
                        prefix=company.openepcis_gcp,
                        why=problem,
                    )
                )
