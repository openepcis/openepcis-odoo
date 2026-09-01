# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""Shared publishing behaviour for every record type that goes to the catalog.

Three rules shape this module, and each is worth stating because breaking any of
them makes the connector unpleasant to live with:

**Publishing is opt-in.** An Odoo database is full of service articles, internal
consumables and one-off contacts that have no business in a public registry.
Nothing leaves without ``openepcis_publish`` being set, and setting it is a
deliberate act on a record or through a mass action.

**Saving never waits for the network.** ``create`` and ``write`` only mark a
record dirty. A scheduled action drains the queue afterwards. Anything else
couples the responsiveness of the ERP to a remote service — and to GS1 behind it.

**Background work records failures instead of raising them.** One unpublishable
product must not abort a batch of two hundred.
"""

import logging
from urllib.parse import quote

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

from ..utils import gs1
from ..utils.exceptions import OpenepcisError

_logger = logging.getLogger(__name__)

#: How many records one scheduled run publishes. Each is a separate HTTP call,
#: and a localized field is read once per installed language, so a modest batch
#: keeps a cron worker from being held for minutes.
CRON_BATCH_SIZE = 50

#: Retryable failures tolerated before a record is parked as an error. Without a
#: ceiling, an unreachable resolver leaves records queued forever and the only
#: sign of trouble is a message nobody looks at.
MAX_ATTEMPTS = 5


class OpenepcisSyncMixin(models.AbstractModel):
    _name = "openepcis.sync.mixin"
    _description = "OpenEPCIS publishing"

    openepcis_publish = fields.Boolean(
        string="Publish to OpenEPCIS",
        copy=False,
        help="Include this record in the OpenEPCIS catalog. Publication happens "
        "in the background shortly after saving.",
    )
    openepcis_state = fields.Selection(
        [
            ("not_synced", "Not published"),
            ("queued", "Waiting"),
            ("synced", "Published"),
            ("error", "Failed"),
        ],
        string="Publication",
        default="not_synced",
        readonly=True,
        copy=False,
        index=True,
    )
    openepcis_last_sync = fields.Datetime(string="Published on", readonly=True, copy=False)
    openepcis_error = fields.Char(string="Last error", readonly=True, copy=False)
    openepcis_attempts = fields.Integer(readonly=True, copy=False, default=0)
    openepcis_digital_link = fields.Char(
        string="Digital Link",
        compute="_compute_openepcis_digital_link",
        help="The GS1 Digital Link this record resolves to.",
    )
    openepcis_qr_src = fields.Char(compute="_compute_openepcis_qr_src")

    # ------------------------------------------------------------------
    # To be provided by each concrete model
    # ------------------------------------------------------------------

    def _openepcis_key(self):
        """The GS1 key identifying this record — a GTIN, a GLN."""
        raise NotImplementedError

    def _openepcis_key_type(self):
        """``GTIN`` or ``GLN``, for check-digit validation."""
        raise NotImplementedError

    def _openepcis_anchor_ai(self):
        """The application identifier this key anchors on in a Digital Link."""
        raise NotImplementedError

    def _openepcis_endpoint(self):
        """Collection path on the resolver, e.g. ``/products``."""
        raise NotImplementedError

    def _openepcis_key_term(self):
        """Name of the key inside the catalog document, e.g. ``gtin``."""
        raise NotImplementedError

    def _openepcis_qualifiers(self):
        """Digital Link qualifiers refining the key: ``{"10": lot}``, ``{"21": serial}``.

        A GS1 key names a class of thing; a qualifier narrows it to a batch or
        a single unit, and the catalog stores a distinct document at each
        level. Every model in this addon is identified by its bare key and
        returns the default; the stock bridge overrides this for lots and
        serial numbers.

        Insertion order is path order, and GS1 prescribes lot (10) before
        serial (21) — an implementation returning both must list them so.
        """
        return {}

    def _openepcis_company(self):
        """Whose credentials publish this record.

        A record without a company belongs to all of them, and the active
        company is then the only sensible answer.
        """
        self.ensure_one()
        company = self["company_id"] if self._fields.get("company_id") else False
        return company or self.env.company

    # ------------------------------------------------------------------
    # Digital Link
    # ------------------------------------------------------------------

    def _openepcis_qualifier_suffix(self):
        """``/{ai}/{value}`` per qualifier, RFC-3986-encoded — or an empty string.

        Shared by the publish path and the Digital Link, so a record can never
        be published under one URI and displayed under another. The values are
        percent-encoded because lot numbers contain whatever people put in lot
        numbers — slashes and spaces included — and a raw slash would silently
        change the path the resolver sees. The AI itself is a GS1 constant and
        needs no encoding.
        """
        self.ensure_one()
        return "".join(
            "/%s/%s" % (ai, quote(str(value), safe=""))
            for ai, value in self._openepcis_qualifiers().items()
        )

    @api.depends("openepcis_state")
    def _compute_openepcis_digital_link(self):
        base = self.env["openepcis.client"].base_url(company=self.env.company)
        for record in self:
            key = record._openepcis_key()
            link = (
                gs1.digital_link(base, record._openepcis_anchor_ai(), key)
                if base and key and record.openepcis_state == "synced"
                else ""
            )
            record.openepcis_digital_link = (
                link + record._openepcis_qualifier_suffix() if link else False
            )

    @api.depends("openepcis_digital_link")
    def _compute_openepcis_qr_src(self):
        """Source for a QR image, rendered by Odoo's own barcode controller.

        Built here rather than in the template because the Digital Link has to
        be percent-encoded to survive a query string, and QWeb has no dependable
        helper for that.
        """
        for record in self:
            link = record.openepcis_digital_link
            record.openepcis_qr_src = (
                "/report/barcode/?barcode_type=QR&value=%s&width=300&height=300&quiet=1"
                % quote(link, safe="")
                if link
                else False
            )

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------

    @api.model
    @tools.ormcache("self._name")
    def _openepcis_watched_fields(self):
        """Odoo fields whose change should re-publish a record.

        Derived from the mapping table so that adding a mapping row is enough —
        no code knows in advance which fields matter. Only the first segment of
        a dotted path counts: a change behind a relation is not seen here, and
        is picked up the next time the record itself is touched.

        Cached because it is consulted on every single write; the mapping model
        clears the cache when a row changes.
        """
        watched = {"openepcis_publish"}
        mappings = (
            self.env["openepcis.field.mapping"].sudo().search([("model_name", "=", self._name)])
        )
        for mapping in mappings:
            watched.add((mapping.odoo_field or "").split(".")[0])
        watched.discard("")
        return watched

    def _openepcis_mark_queued(self):
        """Mark for publication, without touching records that opted out."""
        candidates = self.filtered(lambda r: r.openepcis_publish and r._openepcis_key())
        if candidates:
            candidates.with_context(openepcis_syncing=True).write(
                {"openepcis_state": "queued", "openepcis_attempts": 0}
            )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get("openepcis_syncing"):
            records._openepcis_mark_queued()
        return records

    def write(self, vals):
        result = super().write(vals)
        # The guard matters: recording the outcome of a publication is itself a
        # write, and without it every success would immediately re-queue.
        if not self.env.context.get("openepcis_syncing") and (
            set(vals) & self._openepcis_watched_fields()
        ):
            self._openepcis_mark_queued()
        return result

    # ------------------------------------------------------------------
    # Readiness for a downstream registry
    # ------------------------------------------------------------------

    def _openepcis_kind(self):
        """The catalog's name for this record type: PRODUCT, ORGANIZATION, PLACE."""
        raise NotImplementedError

    @api.model
    @tools.ormcache("self._name")
    def _openepcis_mapping_by_term(self):
        """Mapping rows grouped by the term they write into, as ids.

        Ids rather than records because an ormcache outlives the environment
        that filled it.
        """
        index = {}
        mappings = (
            self.env["openepcis.field.mapping"].sudo().search([("model_name", "=", self._name)])
        )
        for mapping in mappings:
            root = (mapping.gs1_path or "").split(".")[0]
            if root.endswith("[]"):
                root = root[:-2]
            index.setdefault(root, []).append(mapping.id)
        return index

    def _openepcis_missing_terms(self):
        """Terms a downstream registry requires that this record does not carry.

        Advisory only. The catalog accepts an incomplete record on purpose — a
        passport is filled in over time and by several people — so this reports
        rather than blocks.
        """
        self.ensure_one()
        required = self.env["openepcis.channel"].required_terms(
            self._openepcis_kind(), company=self._openepcis_company()
        )
        if not required:
            return []

        index = self._openepcis_mapping_by_term()
        mapping_model = self.env["openepcis.field.mapping"].sudo()
        missing = []
        for term in sorted(required):
            rows = mapping_model.browse(index.get(term) or []).exists()
            if not rows or not any(row._has_value(self) for row in rows):
                missing.append(term)
        return missing

    @api.depends("openepcis_publish")
    def _compute_openepcis_missing_terms(self):
        for record in self:
            try:
                missing = record._openepcis_missing_terms()
            except NotImplementedError:
                missing = []
            record.openepcis_missing_terms = ", ".join(missing)

    openepcis_missing_terms = fields.Char(
        string="Still needed",
        compute="_compute_openepcis_missing_terms",
        help="Fields a downstream GS1 registry asks for and this record does "
        "not yet carry. Publishing is still allowed — the catalog accepts an "
        "incomplete record and expects it to be completed over time.",
    )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def _openepcis_payload(self):
        """The catalog document for this record, key included."""
        self.ensure_one()
        payload = self.env["openepcis.field.mapping"].build_payload(self)
        payload[self._openepcis_key_term()] = self._openepcis_catalog_key()
        return payload

    def _openepcis_catalog_key(self):
        """The key as the catalog addresses it — and a GTIN is fourteen digits.

        The resource is ``/products/{gtin}``, and the catalog compares the key
        in the path against the one in the body: a record written under
        ``9520000000004`` is a different resource from the same product written
        under ``09520000000004``. The Java connector pads
        (``Gs1Keys.padToGtin14``) and this one did not, so the two of them would
        maintain one product as two.

        Only a GTIN. A GLN is thirteen digits and stays thirteen; padding it
        would invent a location.
        """
        self.ensure_one()
        key = gs1.clean(self._openepcis_key())
        return gs1.gtin14(key) if self._openepcis_key_type() == "GTIN" else key

    @api.model
    def _openepcis_phrase_key_problem(self, problem):
        """Turn a :class:`~..utils.gs1.KeyProblem` into a sentence for a user.

        Lives here rather than in the GS1 helpers because a message has to pass
        through Odoo's translation machinery, and those helpers are deliberately
        Odoo-free so they can be tested without a database. Shared by every model
        that carries a key.
        """
        if problem.fault == gs1.EMPTY:
            return _("No %s on this record.", problem.kind)
        if problem.fault == gs1.NOT_NUMERIC:
            return _("A %s is digits only.", problem.kind)
        if problem.fault == gs1.BAD_LENGTH:
            lengths = [str(length) for length in problem.allowed_lengths]
            expected = (
                lengths[0]
                if len(lengths) == 1
                else _("%(list)s or %(last)s", list=", ".join(lengths[:-1]), last=lengths[-1])
            )
            return _(
                "A %(kind)s is %(expected)s digits long; this one has %(actual)s.",
                kind=problem.kind,
                expected=expected,
                actual=problem.actual_length,
            )
        if problem.fault == gs1.BAD_CHECK_DIGIT:
            return _(
                "The %(kind)s fails its check digit — it should end in %(digit)s.",
                kind=problem.kind,
                digit=problem.correct_check_digit,
            )
        return _("The %s is not valid.", problem.kind)

    def _openepcis_check_ready(self):
        """Why this record cannot be published, or an empty string."""
        self.ensure_one()
        problem = gs1.problem_with(self._openepcis_key(), self._openepcis_key_type())
        return self._openepcis_phrase_key_problem(problem) if problem else ""

    def _openepcis_publish_one(self):
        """Publish one record. Returns a terminal error message, or an empty string.

        A failure from the resolver is *not* caught here: whether it is worth
        retrying is information only :class:`OpenepcisError` carries, and
        flattening it to a string would turn every gateway timeout into a
        permanent failure. The caller decides.

        Uses ``PUT``, which the catalog treats as create-or-update, so a repeat
        is harmless and a retry cannot produce a duplicate. ``POST`` would answer
        409 the second time round.

        Note that ``PUT`` merges rather than replaces: a field cleared in Odoo
        stays put in the catalog, because an absent key means "leave alone".
        Clearing a published value is a deliberate act, not a side effect of a
        sync, and is not attempted here.
        """
        self.ensure_one()
        blocker = self._openepcis_check_ready()
        if blocker:
            return blocker

        key = self._openepcis_catalog_key()
        path = "%s/%s%s" % (
            self._openepcis_endpoint(),
            key,
            self._openepcis_qualifier_suffix(),
        )
        self.env["openepcis.client"].put(
            path, self._openepcis_payload(), company=self._openepcis_company()
        )
        return ""

    def _openepcis_record_success(self):
        self.with_context(openepcis_syncing=True).write(
            {
                "openepcis_state": "synced",
                "openepcis_last_sync": fields.Datetime.now(),
                "openepcis_error": False,
                "openepcis_attempts": 0,
            }
        )
        # A drawn identifier is registered with GS1 only now, once the record
        # that justifies it demonstrably exists. Models that never draw one
        # simply do not carry this method.
        if hasattr(self, "_openepcis_confirm_key"):
            self._openepcis_confirm_key()

    def _openepcis_record_failure(self, message, retryable):
        """Keep a record queued while it is worth another try, then park it."""
        for record in self:
            attempts = record.openepcis_attempts + 1
            exhausted = attempts >= MAX_ATTEMPTS
            record.with_context(openepcis_syncing=True).write(
                {
                    "openepcis_state": "queued" if retryable and not exhausted else "error",
                    "openepcis_error": message,
                    "openepcis_attempts": attempts,
                }
            )

    def _openepcis_sync(self, commit=False):
        """Publish these records, recording outcomes rather than raising.

        ``commit`` is for the scheduled action only, where each record is
        committed on its own so that trouble late in a batch does not roll back
        the successes before it — a run that publishes forty of fifty records
        should keep the forty. An interactive call leaves the transaction alone:
        committing halfway through a user's request would make a button press
        partly irreversible.
        """
        for record in self:
            try:
                message = record._openepcis_publish_one()
            except OpenepcisError as exc:
                record._openepcis_record_failure(str(exc), exc.is_retryable)
            except Exception as exc:  # noqa: BLE001 - a batch must survive one bad record
                _logger.exception("Publishing %s#%s failed", record._name, record.id)
                record._openepcis_record_failure(str(exc), False)
            else:
                if message:
                    record._openepcis_record_failure(message, False)
                else:
                    record._openepcis_record_success()

            if commit:
                self.env.cr.commit()  # pylint: disable=invalid-commit

    @api.model
    def _openepcis_cron_sync(self):
        """Scheduled action: drain the queue for this model."""
        if not self.env["openepcis.client"].is_configured():
            return
        if not self.env.company.openepcis_enabled:
            return
        queued = self.search(
            [("openepcis_state", "=", "queued"), ("openepcis_publish", "=", True)],
            limit=CRON_BATCH_SIZE,
            order="write_date asc",
        )
        if queued:
            _logger.info("OpenEPCIS: publishing %s %s record(s)", len(queued), self._name)
            # Committing per record is right in a cron and forbidden in a test,
            # where the whole run lives inside one savepoint.
            queued._openepcis_sync(commit=not tools.config["test_enable"])

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_openepcis_publish(self):
        """Publish now, and say what happened. Interactive counterpart of the cron."""
        if not self.env["openepcis.client"].is_configured():
            raise UserError(
                _(
                    "OpenEPCIS is not configured yet — set the resolver URL and "
                    "credentials under Settings > General Settings > OpenEPCIS."
                )
            )
        # Publishing by hand is an explicit instruction, so opting the record in
        # is part of it rather than a precondition the user has to discover.
        self.filtered(lambda r: not r.openepcis_publish).write({"openepcis_publish": True})
        self._openepcis_sync()

        failed = self.filtered(lambda r: r.openepcis_state == "error")
        if failed:
            if len(self) == 1:
                raise UserError(failed.openepcis_error)
            message = _(
                "%(ok)s of %(total)s published. First failure: %(why)s",
                ok=len(self) - len(failed),
                total=len(self),
                why=failed[0].openepcis_error,
            )
            level = "warning"
        else:
            message = _("%s record(s) published.", len(self))
            level = "success"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": _("OpenEPCIS"), "message": message, "type": level, "sticky": False},
        }

    def action_openepcis_open_digital_link(self):
        self.ensure_one()
        if not self.openepcis_digital_link:
            raise UserError(_("This record has not been published yet."))
        return {
            "type": "ir.actions.act_url",
            "url": self.openepcis_digital_link,
            "target": "new",
        }
