# Part of the OpenEPCIS connector for Odoo. See LICENSE (LGPL-3).
"""``event_uuid`` becomes ``idem_key``: the same value, a different job.

The column held a UUIDv5 derived from the transfer, the business step and the
identifiers, and it was the event's ``eventID``. It is not any more — the
eventID is the canonical CBV hash the repository computes — but as the
database's own handle on a movement it has already reported, the value is
exactly right and every existing row keeps it.

Renaming rather than adding: left to the ORM, a new column would appear empty
beside a full one, and the unique index that keeps a movement from being queued
twice would be built over nothing.

``event_hash`` — the comparison value the inbox recognises our own events by —
is deliberately left empty for existing rows. It could only be recomputed from
the stored payload, and a row that has already been delivered has nothing to
recognise: its echo, if it comes, is about a movement whose story is already
told here.
"""


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'openepcis_event' AND column_name = 'event_uuid'
        """
    )
    if not cr.fetchone():
        return
    cr.execute("ALTER TABLE openepcis_event RENAME COLUMN event_uuid TO idem_key")
    # The old constraint travels with the old name; the ORM adds the new one.
    cr.execute(
        "ALTER TABLE openepcis_event DROP CONSTRAINT IF EXISTS openepcis_event_event_uuid_unique"
    )
