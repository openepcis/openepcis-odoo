# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Where an event happened — the part of EPCIS an ERP usually cannot answer.

An event without a read point is a claim that something happened and no
statement about where, which is half an event. EPCIS wants an SGLN, so a
location needs a GLN, and Odoo's locations have none.

They do have a tree, and that is the way out of asking anybody to number three
hundred shelves. A GLN is set where it means something — the warehouse, the
loading bay, the shop floor — and everything underneath inherits it by walking
up. One number makes a whole site answerable.

Odoo's own GS1 barcode nomenclature already binds AI 414 to a location, so a
GLN written on a location is also what a scanner reads off a GS1 location
label. The two facts are the same fact, and this keeps them in one field.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..vendored import gs1


class StockLocation(models.Model):
    _inherit = "stock.location"

    openepcis_gln = fields.Char(
        string="GLN",
        help="Global Location Number of this place. Sub-locations inherit it, "
        "so it belongs on a site or a bay rather than on every shelf.",
    )
    openepcis_read_point_gln = fields.Char(
        string="Read point",
        compute="_compute_openepcis_read_point_gln",
        recursive=True,
        help="The GLN an event recorded here would carry — this location's own, "
        "or the nearest one above it.",
    )

    @api.constrains("openepcis_gln")
    def _check_openepcis_gln(self):
        for location in self:
            if not location.openepcis_gln:
                continue
            problem = gs1.problem_with(location.openepcis_gln, "GLN")
            if problem:
                raise ValidationError(
                    _(
                        "'%(gln)s' is not a usable GLN. %(why)s",
                        gln=location.openepcis_gln,
                        why=self.env["openepcis.sync.mixin"]._openepcis_phrase_key_problem(problem),
                    )
                )

    # Recursive on purpose: a location's read point is its own GLN or the one
    # above it, so the field depends on itself one level up. Odoo needs telling,
    # or it plans the recomputation as if the chain were flat.
    @api.depends("openepcis_gln", "location_id.openepcis_read_point_gln")
    def _compute_openepcis_read_point_gln(self):
        for location in self:
            location.openepcis_read_point_gln = location._openepcis_read_point()

    def _openepcis_read_point(self):
        """The GLN to record for something observed here.

        Walks up until it finds one. Returns an empty string rather than
        guessing: an event with somebody else's read point is worse than an
        event with none, and the operation type says which it would rather have.
        """
        self.ensure_one()
        node = self
        while node:
            if node.openepcis_gln:
                return gs1.clean(node.openepcis_gln)
            node = node.location_id
        return ""
