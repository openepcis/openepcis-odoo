# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""First load: getting an existing catalogue into OpenEPCIS in one go.

Publishing record by record is right for day-to-day work and wrong for the first
day, when a database holds ten thousand products and each one is a round trip.
The resolver has a bulk endpoint that takes a CSV; this wizard produces it.

Three things about that endpoint shape this module:

**It creates, it does not update.** A key the catalog already holds comes back as
``DUPLICATE_GTIN`` rather than being overwritten. So this is an onboarding tool,
not a synchroniser — for anything after the first load, the ordinary queue is
both correct and safe.

**It caps the upload at 10 MB**, so large catalogues are sent in chunks.

**Its column names are load-bearing.** The parser looks them up by name and
silently drops what it does not recognise, so a header that is nearly right
produces a run where every row fails validation for no visible reason.
"""

import csv
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..utils.exceptions import OpenepcisError
from ..utils.gs1 import is_valid

_logger = logging.getLogger(__name__)

#: Well under the resolver's 10 MB limit, so a chunk cannot be refused for size.
CHUNK_ROWS = 2000

#: Exactly the columns ProductCsvParser and OrganizationCsvParser look for.
#: Changing a name here silently breaks every row — see the module docstring.
PRODUCT_COLUMNS = [
    "gtin",
    "productName_en",
    "gpcCategoryCode",
    "brandName",
    "countryOfOriginCode",
    "hasBatchLotNumber",
    "hasSerialNumber",
    "isAnonymousAccessAllowed",
]
ORGANIZATION_COLUMNS = [
    "globalLocationNumber",
    "organizationName_en",
    "glnType",
    "organizationRole",
    "partyGLN",
    "department_en",
]


class OpenepcisBulkImport(models.TransientModel):
    _name = "openepcis.bulk.import"
    _description = "Load an existing catalogue into OpenEPCIS"

    kind = fields.Selection(
        [("product", "Products"), ("organization", "Contacts")],
        required=True,
        default="product",
    )
    scope = fields.Selection(
        [
            ("marked", "Records marked for publication"),
            ("all", "Every record with an identifier"),
        ],
        required=True,
        default="marked",
    )
    record_count = fields.Integer(compute="_compute_record_count", string="Records")
    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft", readonly=True)
    result_summary = fields.Text(readonly=True)
    error_detail = fields.Text(readonly=True)

    @api.depends("kind", "scope")
    def _compute_record_count(self):
        for wizard in self:
            wizard.record_count = len(wizard._records())

    # ------------------------------------------------------------------

    def _records(self):
        """The records this run would send, in a stable order."""
        self.ensure_one()
        if self.kind == "product":
            domain = [("barcode", "!=", False)]
            model = "product.product"
        else:
            domain = [("openepcis_gln", "!=", False), ("is_company", "=", True)]
            model = "res.partner"
        if self.scope == "marked":
            domain.append(("openepcis_publish", "=", True))
        return self.env[model].search(domain, order="id")

    def _english(self, record, field_name):
        """The English text for a field, since the bulk columns are English-only.

        The endpoint's CSV format has one column per term and no way to express
        a language map, so it hardcodes ``en``. Anything else has to wait for the
        ordinary per-record publication, which does carry every language.
        """
        for lang in ("en_US", "en_GB", self.env.context.get("lang"), "en"):
            if not lang:
                continue
            value = record.with_context(lang=lang)[field_name]
            if value:
                return value
        return record[field_name] or ""

    def _rows(self, records):
        if self.kind == "product":
            for product in records:
                yield {
                    "gtin": product.barcode,
                    "productName_en": self._english(product, "name"),
                    "gpcCategoryCode": product.categ_id.openepcis_gpc_code or "",
                    "brandName": product.product_tmpl_id.openepcis_brand_name or "",
                    "countryOfOriginCode": (
                        product.product_tmpl_id.openepcis_country_of_origin_id.code or ""
                    ),
                    "hasBatchLotNumber": "",
                    "hasSerialNumber": "",
                    "isAnonymousAccessAllowed": "",
                }
        else:
            for partner in records:
                yield {
                    "globalLocationNumber": partner.openepcis_gln,
                    "organizationName_en": self._english(partner, "name"),
                    "glnType": "",
                    "organizationRole": "",
                    "partyGLN": "",
                    "department_en": "",
                }

    def _csv(self, rows):
        columns = PRODUCT_COLUMNS if self.kind == "product" else ORGANIZATION_COLUMNS
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        return buffer.getvalue().encode("utf-8")

    # ------------------------------------------------------------------

    def action_import(self):
        self.ensure_one()
        records = self._records()
        if not records:
            raise UserError(_("Nothing to send — no record matches."))

        endpoint = "/bulk/products" if self.kind == "product" else "/bulk/organizations"
        key_field = "barcode" if self.kind == "product" else "openepcis_gln"
        key_type = "GTIN" if self.kind == "product" else "GLN"

        # A malformed key would fail server-side anyway, one opaque row at a
        # time. Catching it here keeps the report about real problems.
        usable = records.filtered(lambda r: is_valid(r[key_field], key_type))
        skipped = records - usable

        totals = {"total": 0, "success": 0, "errors": 0}
        problems = []
        published = records.browse()

        for start in range(0, len(usable), CHUNK_ROWS):
            chunk = usable[start : start + CHUNK_ROWS]
            payload = self._csv(self._rows(chunk))
            try:
                result = self.env["openepcis.client"].post_file(
                    endpoint, "openepcis-import.csv", payload, form={"format": "csv"}
                )
            except OpenepcisError as exc:
                raise UserError(
                    _(
                        "The upload failed after %(done)s of %(total)s records: %(why)s",
                        done=totals["total"],
                        total=len(usable),
                        why=exc,
                    )
                ) from exc

            totals["total"] += result.get("total") or 0
            totals["success"] += result.get("successCount") or 0
            totals["errors"] += result.get("errorCount") or 0
            problems.extend(result.get("errors") or [])
            published |= self._mark(chunk, result)

        return self._report(totals, problems, skipped)

    def _mark(self, chunk, result):
        """Record the outcome per row, so the queue does not redo the work.

        A duplicate is not a failure here: it means the catalog already holds
        that key, which is the state this wizard was trying to reach.
        """
        failed_rows = set()
        for problem in result.get("errors") or []:
            if problem.get("errorCode") in ("DUPLICATE_GTIN", "DUPLICATE_GLN"):
                continue
            row = problem.get("rowNumber")
            if row:
                failed_rows.add(row)

        succeeded = chunk.browse()
        for index, record in enumerate(chunk, start=1):
            if index in failed_rows:
                continue
            succeeded |= record
        if succeeded:
            succeeded.with_context(openepcis_syncing=True).write(
                {
                    "openepcis_publish": True,
                    "openepcis_state": "synced",
                    "openepcis_last_sync": fields.Datetime.now(),
                    "openepcis_error": False,
                    "openepcis_attempts": 0,
                }
            )
        return succeeded

    def _report(self, totals, problems, skipped):
        lines = [
            _("%(ok)s of %(total)s rows accepted.", ok=totals["success"], total=totals["total"])
        ]
        if skipped:
            lines.append(
                _(
                    "%s record(s) were left out because their identifier is not "
                    "a valid GS1 key.",
                    len(skipped),
                )
            )
        duplicates = sum(
            1 for p in problems if p.get("errorCode") in ("DUPLICATE_GTIN", "DUPLICATE_GLN")
        )
        if duplicates:
            lines.append(
                _(
                    "%s were already in the catalog. Bulk loading only creates; "
                    "those records are counted as published.",
                    duplicates,
                )
            )

        detail = "\n".join(
            "row %s: %s — %s"
            % (p.get("rowNumber"), p.get("errorCode"), p.get("errorMessage") or "")
            for p in problems
            if p.get("errorCode") not in ("DUPLICATE_GTIN", "DUPLICATE_GLN")
        )
        if skipped:
            detail = "\n".join(
                filter(
                    None,
                    [
                        detail,
                        _("Left out:"),
                        "\n".join("  %s" % record.display_name for record in skipped[:50]),
                    ],
                )
            )

        self.write(
            {
                "state": "done",
                "result_summary": "\n".join(lines),
                "error_detail": detail or False,
            }
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
