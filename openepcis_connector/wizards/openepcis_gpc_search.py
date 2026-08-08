# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Finding a GPC brick without knowing eight digits by heart.

``gpcCategoryCode`` is required by GS1 registries and there are several thousand
bricks, so a free-text box is not a usable way to ask for one. The resolver
offers the classification as a searchable index; this wizard is the Odoo end of
it.

Deliberately a wizard rather than an autocomplete widget: a widget means OWL
components, and the OWL API moved between Odoo 18 and 19. A wizard is plain
Python and XML, works identically in both, and — since a brick is chosen once
per product category and then never again — costs the user one extra click on a
rare action.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils.exceptions import OpenepcisError

RESULT_LIMIT = 40


class OpenepcisGpcSearch(models.TransientModel):
    _name = "openepcis.gpc.search"
    _description = "Search the GS1 product classification"

    category_id = fields.Many2one("product.category", required=True, readonly=True)
    query = fields.Char(
        string="Search",
        help="Words from the brick name or definition, or an eight-digit code.",
    )
    result_ids = fields.One2many("openepcis.gpc.search.result", "search_id", readonly=True)
    searched = fields.Boolean(readonly=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if self.env.context.get("active_model") == "product.category":
            category = self.env["product.category"].browse(self.env.context.get("active_id"))
            values["category_id"] = category.id
            # Seed the box with the category name: nine times out of ten the
            # brick is called something close to it.
            values.setdefault("query", category.name)
        return values

    def action_search(self):
        self.ensure_one()
        if not (self.query or "").strip():
            raise UserError(_("Type something to search for."))

        try:
            nodes = self.env["openepcis.client"].get(
                "/gpc/search",
                params={"q": self.query.strip(), "level": "BRICK", "size": RESULT_LIMIT},
            )
        except OpenepcisError as exc:
            raise UserError(_("The classification could not be searched: %s", exc)) from exc

        self.result_ids.unlink()
        self.env["openepcis.gpc.search.result"].create(
            [
                {
                    "search_id": self.id,
                    "code": node.get("code"),
                    "title": node.get("title"),
                    "definition": node.get("definition"),
                    "lineage": node.get("lineage") or node.get("path"),
                }
                for node in (nodes or [])
                if node.get("code")
            ]
        )
        self.searched = True
        return self._reopen()

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }


class OpenepcisGpcSearchResult(models.TransientModel):
    _name = "openepcis.gpc.search.result"
    _description = "A GPC brick offered by the search"
    _order = "code"

    search_id = fields.Many2one("openepcis.gpc.search", required=True, ondelete="cascade")
    code = fields.Char(readonly=True)
    title = fields.Char(readonly=True)
    definition = fields.Text(readonly=True)
    lineage = fields.Char(string="Segment / family / class", readonly=True)

    def action_choose(self):
        """Put this brick on the category the search started from."""
        self.ensure_one()
        self.search_id.category_id.write(
            {"openepcis_gpc_code": self.code, "openepcis_gpc_title": self.title}
        )
        return {"type": "ir.actions.act_window_close"}
