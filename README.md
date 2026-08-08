# OpenEPCIS Connector for Odoo

Publishes Odoo master data to an [OpenEPCIS](https://openepcis.io) catalog behind
a GS1-conformant Digital Link resolver, and brings the resulting Digital Link and
QR code back onto the Odoo form.

The data a Digital Product Passport is built from — product name, brand, weight,
country of origin, manufacturer — is already in the ERP. This addon stops it from
being typed a second time.

| | |
|---|---|
| **Licence** | LGPL-3 |
| **Odoo** | 18.0 (branch `18.0`), 19.0 (branch `19.0`) |
| **Dependencies** | `product` only. No Python packages beyond what Odoo ships. |
| **Direction** | Odoo → OpenEPCIS. See [Limits](#limits). |

---

## What it does

- **Products and contacts are queued on save** and published in the background by
  a scheduled action. Saving a product never waits for the network.
- **Publishing is opt-in.** Nothing leaves the database until someone ticks
  *Publish to OpenEPCIS* on a record or runs the mass action.
- **The field mapping is data, not code.** Which Odoo field feeds which GS1 term
  is a list of records an administrator can edit — because no two Odoo databases
  keep a brand in the same place.
- **Identifiers can be drawn from your own GS1 company prefix**, so a product
  without a barcode is one button away from having a real GTIN.
- **The Digital Link and its QR code** appear on the product form and on a
  printable label.

Publication onward to a GS1 national registry happens on the OpenEPCIS side. The
connector writes to the catalog; the platform forwards. It never talks to GS1
directly.

---

## Installation

```bash
git clone https://github.com/openepcis/openepcis-odoo.git
# put openepcis_connector on your addons path, then:
odoo -d yourdb -i openepcis_connector
```

Then **Settings → General Settings → OpenEPCIS**: enter the resolver URL, the API
key and the secret, and press **Test connection**.

---

## Before it will work: the Keycloak side

This is the part that costs an afternoon, and none of it is Odoo work. The API
key stands for an identity in the platform's Keycloak realm, and that identity
must carry three things or the resolver will refuse it:

| What | Why | Symptom when missing |
|---|---|---|
| Realm role named after your tenant (or `<tenant>/create`, `<tenant>/update`) | Authorises writes | `403` on every publish |
| Claim `defaultGroup` | Names the tenant whose catalog is written | `400 … carries no defaultGroup claim` |
| Claim `gs1CompanyPrefix` | Bounds which GTINs and GLNs you may write | `403` on keys outside your prefix |

Optional, and only if you publish product images: the realm role `files-writer`.

**Test connection** probes for each of these in turn and names the one that is
missing, so you do not have to read resolver logs to find out.

If your platform operator issues bearer tokens instead of API keys, a Keycloak
service account using `client_credentials` works identically — the resolver
accepts both and derives the same identity.

---

## Getting a product published

1. Give the product a **barcode**. It is the GTIN. Each variant is its own trade
   item and needs its own.
   *No barcode?* Use **Draw GTIN** to take the next free number from your company
   prefix.
2. Set the **GPC brick** on the product category — once per category, not per
   product. The picker searches GS1's classification by name.
3. Fill in what a registry requires. The *GS1 / OpenEPCIS* page shows the list
   and ticks items off as you go: brand, GPC brick, net content, a description,
   and at least one target market.
4. Tick **Publish to OpenEPCIS** and save. Within five minutes the state turns to
   *Published* and the Digital Link appears.

Impatient? **Publish now** does it immediately and reports the outcome.

---

## Loading an existing catalogue

**Settings → Technical → OpenEPCIS → First load.**

Publishing ten thousand products one HTTP call at a time takes an afternoon. The
wizard sends them as a single CSV upload instead, in chunks.

It is a first-load tool and nothing more: the endpoint behind it **creates**
records and does not update them, so anything the catalog already holds comes
back as a duplicate and is left alone. It also carries only the English name —
the bulk format has one column per term and no way to express a language map.
Everything published later through the ordinary queue carries every language and
every mapped field.

---

## The field mapping

**Settings → Technical → OpenEPCIS → Field mapping.**

Each row says which Odoo field feeds which term of the published document.
Dotted paths work on both sides:

| Odoo field | GS1 term | Meaning |
|---|---|---|
| `name` | `productName` | Localised — one entry per installed language |
| `categ_id.openepcis_gpc_code` | `gpcCategoryCode` | Reads across a relation |
| `openepcis_brand_name` | `brand.brandName` | Writes into a nested object |
| `openepcis_target_market_ids.code` | `targetMarket[].targetMarketCountries.countryCode` | `[]` builds a list |

The shipped rows are starting points, marked `noupdate` so an upgrade will not
undo your edits. Two worth knowing about:

- **Country of origin** points at a field this addon adds, not at Odoo's
  `country_of_origin` — that one comes from the Intrastat module, which is
  Enterprise. If you run it, repoint the row and delete the spare field.
- **Weight** is sent as `KGM`. If your database is configured in pounds, change
  the fixed unit on that row to `LBR`.

---

## Limits

**One direction.** Changes made in the OpenEPCIS web UI do not come back to Odoo.
The platform's master-data events do not leave the resolver process — there is no
webhook to subscribe to — so a return channel would need work on the platform
side, not here.

**`PUT` merges, it does not replace.** Clearing a field in Odoo leaves the
published value in place, because an absent key means "leave alone" to the
catalog. Removing a published value is a deliberate act and this addon does not
do it as a side effect of a sync.

**Places and EPCIS events are out of scope.** Warehouses as GS1 places, and stock
moves as EPCIS events, are separate undertakings.

---

## ⚠️ Drawing identifiers is not a dry run

**There is no GS1 sandbox.** The platform's GS1 credentials are production
credentials, and a registered identifier is registered for good — GS1 will
neither delete nor deactivate a key that has no product data behind it.

The connector is built around that fact: **Draw GTIN** only *holds* a number
locally, and it is registered upstream at the moment the record is saved. A
number you drew and then abandoned can be handed back; a number that has been
confirmed cannot.

Before testing against anything other than a development deployment, check with
your platform operator that `OPENEPCIS_GS1DE_DRY_RUN` is `true`. And use the
**952** prefix for anything experimental — GS1 reserves it for exactly this.

---

## Development

```bash
# A local Odoo with the addon mounted, on http://localhost:8069
docker compose up -d

# The test suite
docker compose run --rm odoo odoo -d test -i openepcis_connector \
  --test-enable --test-tags /openepcis_connector --stop-after-init

# Lint and formatting, as CI runs them
ruff check . && ruff format --check .
```

The tests never touch a real resolver. They stub the HTTP client, because a
suite that talked to a live platform would depend on a network, on credentials,
and — since a confirmed identifier is registered with GS1 for good — on nothing
ever going wrong. The live check is the manual smoke test above, against a
development deployment, with 952-prefix identifiers.

`utils/gs1.py` is written against the GS1 General Specifications — section 7.9
for the check digit, the key-format tables for permitted lengths — and against
nothing else. It shares no code with any server-side component, which is what
lets this addon carry its own licence without qualification. Its tests verify the
arithmetic against the property the specification states rather than against the
module's own output, and they need no database, which is why CI runs them first.

Keys are validated in the addon **and** again by the resolver. That is
deliberate: catching a mistyped GTIN while the person who typed it is still
looking at it is worth a little duplication, and the resolver remains the
authority.

### Branches

`18.0` and `19.0`, following Odoo's convention: one branch per supported
release, with the same code and only the version-specific differences between
them. Business logic is deliberately version-neutral Python and the views avoid
custom JavaScript — the GPC picker is a wizard rather than an autocomplete
widget for exactly this reason — so a port stays cheap.
