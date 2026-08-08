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
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..utils import gs1


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

    # Odoo 19 declares table constraints this way; the 18.0 branch still uses
    # _sql_constraints, which 19 accepts silently and then ignores — meaning the
    # constraint would simply not exist and two parties could share a GLN.
    _openepcis_gln_unique = models.Constraint(
        "unique (openepcis_gln)",
        "A GLN identifies exactly one party — this one is already in use.",
    )

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
