# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Contacts as GS1 organizations.

A product passport names a manufacturer, and a manufacturer is a party with a
GLN. That party is an Odoo contact, so this is the second thing worth publishing
after products themselves.

Two details that are easy to get wrong:

**A party anchors on AI 417, not 414.** Both are GLNs, but 414 identifies a
physical location and 417 identifies the party that operates it. The resolver
routes them separately and has no ``/414`` route for organizations.

**Only companies.** An individual contact is not an organization, and publishing
one would put a person's name and address into a registry — which is neither
correct nor something anyone asked for.

Organizations also travel the other way. When another system (a second CRM, the
resolver's own editor, an import from GS1) changes an organization in the
catalog, a scheduled action brings the change into the contact with that GLN,
or creates the contact. The catalog does not yet say *when* a record changed,
so each run walks the tenant's organizations and compares a digest of each
with the one it saw last time; that is cheap for the few hundred parties a
tenant holds.
"""

import hashlib
import json
import logging

from odoo import _, api, fields, models, tools
from odoo.exceptions import ValidationError

from ..utils import gs1
from ..utils.exceptions import OpenepcisError
from ..vendor.openepcis_client.core.errors import OpenEpcisError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _name = "res.partner"
    _inherit = ["res.partner", "openepcis.sync.mixin", "openepcis.key.pool.mixin"]

    openepcis_gln = fields.Char(
        string="GLN",
        size=13,
        copy=False,
        index="btree_not_null",
        help="Global Location Number identifying this party. Thirteen digits, "
        "the last of which is a check digit.",
    )

    openepcis_pull_digest = fields.Char(
        readonly=True,
        copy=False,
        help="Digest of the catalog record as last read back. A record whose "
        "digest is unchanged is not looked at again.",
    )

    _sql_constraints = [
        (
            "openepcis_gln_unique",
            "unique(openepcis_gln)",
            "A GLN identifies exactly one party — this one is already in use.",
        ),
    ]

    @api.constrains("openepcis_gln")
    def _check_openepcis_gln(self):
        """Reject a malformed GLN at entry rather than at publication.

        Catching it here means the person who typed it is still looking at it.
        """
        for partner in self:
            if not partner.openepcis_gln:
                continue
            problem = gs1.problem_with(partner.openepcis_gln, "GLN")
            if problem:
                raise ValidationError(
                    _(
                        "'%(gln)s' is not a usable GLN. %(why)s",
                        gln=partner.openepcis_gln,
                        why=self._openepcis_phrase_key_problem(problem),
                    )
                )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _openepcis_key(self):
        self.ensure_one()
        return gs1.clean(self.openepcis_gln)

    def _openepcis_key_type(self):
        return "GLN"

    def _openepcis_kind(self):
        return "ORGANIZATION"

    def _openepcis_key_field(self):
        return "openepcis_gln"

    def _openepcis_draw_ai(self):
        return gs1.ANCHOR_AI["PARTY_GLN"]

    def _openepcis_anchor_ai(self):
        return gs1.ANCHOR_AI["PARTY_GLN"]

    def _openepcis_endpoint(self):
        return "/organizations"

    def _openepcis_key_term(self):
        return "globalLocationNumber"

    def _openepcis_check_ready(self):
        if not self.is_company:
            return _(
                "Only companies are published as organizations — %s is an individual.",
                self.display_name,
            )
        return super()._openepcis_check_ready()

    @api.model
    def _openepcis_cron_sync(self):
        """Scheduled action entry point, kept here so the cron names a real model."""
        return super()._openepcis_cron_sync()

    # ------------------------------------------------------------------
    # Reading back from the catalog
    # ------------------------------------------------------------------

    @api.model
    def _openepcis_cron_pull(self):
        """Scheduled action: bring organizations changed in the catalog into Odoo.

        Runs per configured company, because each company reads with its own
        credentials and so sees its own tenant. One company that cannot reach
        the resolver is logged and skipped; the others still run.
        """
        client = self.env["openepcis.client"]
        for company in self.env["res.company"].sudo().search([]):
            if not client.is_configured(company):
                continue
            try:
                counts = self.with_company(company)._openepcis_pull(company)
            except OpenepcisError as exc:
                _logger.warning(
                    "OpenEPCIS: reading organizations for %s failed: %s", company.name, exc
                )
                continue
            if any(counts.values()):
                _logger.info("OpenEPCIS: organizations read back for %s: %s", company.name, counts)

    @api.model
    def _openepcis_pull(self, company):
        """Walk the tenant's organizations once; returns what happened, counted.

        A contact waiting to be published is left alone: its local edit is
        newer than anything the catalog holds and goes out on the next publish
        run, after which this run sees the catalog agree with it.
        """
        client = self.env["openepcis.client"]
        masterdata = client.masterdata(company)
        mapping = self.env["openepcis.field.mapping"]
        counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        try:
            for document in masterdata.iter_organizations():
                gln = gs1.clean(str(document.get("globalLocationNumber") or ""))
                if gs1.problem_with(gln, "GLN"):
                    counts["skipped"] += 1
                    continue
                digest = self._openepcis_digest(document)
                partner = self.with_context(active_test=False).search(
                    [("openepcis_gln", "=", gln)], limit=1
                )
                if partner and (
                    partner.openepcis_pull_digest == digest or partner.openepcis_state == "queued"
                ):
                    counts[
                        "unchanged" if partner.openepcis_pull_digest == digest else "skipped"
                    ] += 1
                    continue

                values = mapping.read_values(self._name, document)
                syncing = self.with_context(openepcis_syncing=True)
                if partner:
                    changed = {
                        name: value
                        for name, value in values.items()
                        if self._openepcis_differs(partner[name], value)
                    }
                    partner.with_context(openepcis_syncing=True).write(
                        {**changed, "openepcis_pull_digest": digest}
                    )
                    counts["updated" if changed else "unchanged"] += 1
                    continue

                if not values.get("name"):
                    counts["skipped"] += 1
                    continue
                syncing.create(
                    {
                        **values,
                        "is_company": True,
                        "openepcis_gln": gln,
                        # Already in the catalog, so already published: a later
                        # edit in Odoo goes back out like any other.
                        "openepcis_publish": True,
                        "openepcis_state": "synced",
                        "openepcis_last_sync": fields.Datetime.now(),
                        "openepcis_pull_digest": digest,
                    }
                )
                counts["created"] += 1
        except OpenEpcisError as exc:
            raise client._adapt(exc) from exc
        if not tools.config["test_enable"]:
            self.env.cr.commit()
        return counts

    @staticmethod
    def _openepcis_digest(document):
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _openepcis_differs(current, value):
        if hasattr(current, "_name"):
            return current.id != value
        return (current or "") != value
