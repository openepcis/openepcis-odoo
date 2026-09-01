<!-- SPDX-License-Identifier: LGPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG -->

# Connector roadmap: master data first, EPCIS later

Phase 0 output. This document classifies the current Odoo addon, records the API
contract it uses, inventories the vocabulary it emits, and proposes the target
architecture for a family of connectors. It contains no implementation. Phase 1
does not start until this is approved.

## What the addon connects to

The addon connects Odoo to benelog's product data platform. The platform runs on
the OpenEPCIS stack and exposes three REST services, each publishing live OpenAPI
3.1 at `/q/openapi`:

| Service | Host (dev) | Role |
|---|---|---|
| Digital Link resolver | `id.dev.epcis.cloud` | Master data (products, organizations, places), GS1 keys, Digital Link resolution. 75 paths. |
| EPCIS 2.0 REST API | `api.dev.epcis.cloud` | Event capture and query (Phase 5). |
| DPP API (EN 18222) | `dpp.dev.epcis.cloud` | Digital Product Passport. |

The connector today talks only to the resolver.

## Five interaction patterns, not one sync

The work splits into five patterns with different reliability requirements. They
are not one generic "sync".

1. GTIN and key allocation from a GS1 licence pool. Stateful, hard to reverse.
   Reserve then commit.
2. Product master data publication as JSON. Idempotent upsert.
3. Digital Link linkset management on the resolver. Partial update, never full
   replace. Ownership attribution per link.
4. Upstream registration to the GS1 Germany Service Platform, including Verified
   by GS1. Mediated call, delegated credentials, two auth modes.
5. EPCIS 2.0 event capture. Append only. Phase 5.

### What is actually implemented today

The audit found that only two and a half of the five patterns are wired to the
platform. This corrects the earlier "EPCIS connector" framing and sets the real
starting point for extraction.

| Pattern | State in the addon |
|---|---|
| registry (key pool) | Implemented: `POST /gs1de/keys/draw`, `DELETE /gs1de/keys/{ai}/{key}`, `POST /gs1de/keys/{ai}/{key}/confirm`. |
| masterdata (upsert) | Implemented: `PUT /products/{gtin}`, `PUT /organizations/{gln}`, `POST /bulk/*`, read-only `GET /sync/channels`, `GET /gpc/search`. |
| resolver (linkset) | Not implemented. Digital Links are constructed locally and displayed. No write to the resolver. Greenfield. |
| upstream / Verified by GS1 | Not called. The addon only reads what a downstream channel requires. Onward publication happens on the platform side. Greenfield. |
| EPCIS | Absent. Phase 5. |

## Target architecture

Two client libraries, split by base framework rather than by target product.
Python serves Odoo and ERPNext. Java serves metasfresh and BaSyx.

```
   Odoo addon        ERPNext app          metasfresh module     BaSyx extension
   (LGPLv3)          (GPLv3)              (GPL-2.0-or-later)    (Apache-2.0)
        \              /                        \                 /
         \            /                          \               /
      benelog-client-python                   benelog-client-java
          (Apache-2.0)                           (Apache-2.0)
                \                                    /
                 \                                  /
                  +----------- benelog APIs --------+
                       Master Data | Resolver | Registry | EPCIS
```

Module layout, identical in both languages:

```
core/        Identifiers, GS1 Digital Link URIs, JSON handling, vocabulary
             manifest, HTTP transport, auth strategies, retry, errors
masterdata/  Product records, GS1 Web Vocabulary terms, upsert, diff
resolver/    Linkset read and patch, linkType handling, ownership attribution
registry/    GTIN allocation, upstream registration, Verified by GS1
epcis/       Capture and query (Phase 5)
```

A connector pulls only the modules it needs. The current Odoo scope is
`core + masterdata + registry`. `resolver` is greenfield.

Layering inside every connector:

1. Host adapter, framework specific, inherits the host licence. Hooks into the
   host ORM, owns the outbox, writes status back.
2. Mapping layer, framework specific. Host domain objects to platform records.
3. Client library, Apache-2.0, zero host dependencies.

The client libraries must not import anything from Odoo, Frappe, metasfresh, or
BaSyx.

## Licensing

The client libraries are Apache-2.0. This is decided. Apache-2.0 gives an
explicit patent grant, is consistent with OpenEPCIS, and is one-way compatible
into GPLv3 and LGPLv3, which covers Odoo (LGPLv3) and ERPNext (GPLv3). metasfresh
is GPL-2.0-or-later; the "or later" clause admits the GPLv3 option, so Apache-2.0
combines cleanly. Verify the specific metasfresh module's LICENSE before Phase 3.

Two hygiene rules:

1. Every dependency of the client libraries must be permissive. A CI licence
   scanner fails the build on copyleft inside the Apache-2.0 packages. Every
   dependency is justified.
2. No copyleft code is copied into the libraries. OCA modules are AGPL-3. GS1
   parsing must not be lifted from `base_gs1_barcode`, `product_gs1_barcode`, or
   any OCA module. Reimplement from the GS1 General Specifications.

SPDX headers everywhere, a `NOTICE` file, REUSE compliance with `reuse lint` in
CI. Contributions under DCO, no CLA.

### Provenance finding

The audit found no OCA or Odoo-core derived code. Every source file carries the
single LGPL-3 header. `depends` is `["product"]` only, with no OCA or Enterprise
dependency. `utils/gs1.py` states its own clean-room status: it shares no code
with any other implementation. The check digit is the GS1 General Specifications
mod-10 arithmetic, an unprotectable mathematical fact, and is implemented
compactly without `stdnum` or any barcode library. `product_template.py` names
OCA `product_brand` only to explain why it is deliberately not depended on.

Action on extraction: swap the LGPL header for an Apache-2.0 header, add a
one-line clean-room note to `gs1.py`, and keep the code as is. Low risk.

## File classification

`odoo` = Odoo framework bound. `mapping` = Odoo to GS1 translation, partly
reusable. `generic` = candidate for `benelog-client-python`.

| Path under `openepcis_connector/` | Class | Library module |
|---|---|---|
| `utils/gs1.py` | generic | core |
| `utils/exceptions.py` | generic | core |
| `models/openepcis_client.py` | mapping | core (transport + auth) |
| `models/openepcis_key_pool.py` | mapping | registry |
| `models/openepcis_channel.py` | mapping | registry |
| `models/openepcis_field_mapping.py` | mapping | masterdata (payload builder) |
| `models/openepcis_sync_mixin.py` | odoo | masterdata (upsert contract only) |
| `models/product_product.py` | odoo | masterdata (binding) |
| `models/product_template.py` | odoo | (Odoo only) |
| `models/res_partner.py` | odoo | masterdata (binding) |
| `models/product_category.py` | odoo | (Odoo only) |
| `models/uom_uom.py` | mapping | masterdata (unit code table) |
| `models/res_company.py` | odoo | core config surface |
| `models/res_config_settings.py` | odoo | (Odoo only) |
| `wizards/openepcis_bulk_import.py` | mapping | masterdata (bulk) |
| `wizards/openepcis_gpc_search.py` | mapping | masterdata (GPC) |
| `data/openepcis_field_mapping_data.xml` | mapping | masterdata (vocab seed) |
| `data/openepcis_partner_mapping_data.xml` | mapping | masterdata (vocab seed) |
| `data/ir_cron_data.xml`, `views/*`, `report/*`, `security/*` | odoo | (Odoo only) |
| `tests/test_gs1.py` | generic | core (plain unittest, DB free) |
| `tests/test_*.py` (rest) | odoo | (Odoo TransactionCase) |

## Vocabulary inventory

The addon emits nested JSON keyed by bare GS1 Web Vocabulary local names. There is
no `@context`, no `gs1:` IRI prefix, and no JSON-LD framing in the outgoing
payload. The `gs1:` prefix appears only on the inbound side, in channel
`requiredTerms` comparison. JSON-LD framing is done server side. The client speaks
term-local JSON.

Standard terms emitted, product (`data/openepcis_field_mapping_data.xml`):
`productName`, `productDescription`, `brand.brandName`, `gpcCategoryCode`,
`netContent` as `{value,unitCode}`, `targetMarket[].targetMarketCountries.countryCode`,
`countryOfOrigin.countryCode`, `netWeight`, `grossVolume`.

Standard terms emitted, organization (`data/openepcis_partner_mapping_data.xml`):
`organizationName`, `address.streetAddress`, `address.streetAddressLine2`,
`address.addressLocality`, `address.postalCode`, `address.addressRegion`,
`address.addressCountry.countryCode`, `contactPoint[].email`,
`contactPoint[].telephone`.

Hard-coded in code, must move into the manifest: `"gtin"`
(`product_product.py:42`), `"globalLocationNumber"` (`res_partner.py:92`), record
kinds `PRODUCT` and `ORGANIZATION`, anchor AIs in `ANCHOR_AI`
(`utils/gs1.py:35-40`).

benelog namespace extension terms: none found. Every emitted term is standard GS1
Web Vocabulary. The manifest work starts from a pure GS1 baseline.

Two divergent payload vocabularies exist: the per-record nested JSON above, and a
flat, English-only bulk CSV column set (`wizards/openepcis_bulk_import.py:39-56`)
that includes `hasBatchLotNumber`, `hasSerialNumber`, `isAnonymousAccessAllowed`,
`glnType`, `organizationRole`, `partyGLN`, `department`, none of which appear in
the per-record mapping. The manifest should unify or explicitly namespace them.

Value shaping that belongs with the manifest: measurement to `{value, unitCode}`
with UN/CEFACT Rec20 codes; string-typed booleans as `"true"`/`"false"`; localized
text as a language map keyed by BCP-47 subtag; a `[]` path segment as a
single-element list.

## Upstream reuse

- Java. `openepcis-epc-digitallink-translator` exists in the OpenEPCIS monorepo
  and is published to Maven Central. `java-epcis-event-hash-generator` covers
  EPCIS event identifiers. `benelog-client-java` reuses both rather than
  reimplementing identifier and Digital Link translation.
- Python. No `openepcis`, `gs1-digital-link`, or `gs1` package is on PyPI (checked,
  all 404). `benelog-client-python` therefore carries its own identifier and
  Digital Link code. That code already exists, clean-room, in `utils/gs1.py`.

To verify before Phase 1 and Phase 3: the exact Maven coordinates and current
version of the Java translator, and whether any newer Python GS1 Digital Link
package has since appeared on PyPI.

## Extraction order

Ranked by readiness, from the audit:

1. `core` GS1 math (`utils/gs1.py`) and `exceptions.py`. Ready now. Zero Odoo
   imports, already DB-free tested (`tests/test_gs1.py`). Header swap only.
2. `core` transport and auth (`openepcis_client.py`). Mostly isolated: the only
   place HTTP happens, every pattern funnels through it. Remove three couplings:
   `_settings()` reading `res.company`, `_(...)` in error strings, and token
   write-back to the model. Introduce `AuthStrategy` and a token-store callback.
3. `masterdata` payload builder (`openepcis_field_mapping.py`). The dotted-path
   assembly and value shaping are self-contained once fed a plain dict. The field
   reads (`_extract`, `_traverse`) stay in the addon.
4. `registry` key pool (`openepcis_key_pool.py`) and channel reader
   (`openepcis_channel.py`). Three HTTP calls plus parsing extract. The Odoo state
   machine stays in the addon.
5. `resolver` linkset. Greenfield.
6. `registry` upstream and Verified by GS1, with customer-token pass-through auth.
   Greenfield, and the auth mode is a prerequisite.
7. `epcis`. Phase 5.

Cross-cutting blockers to resolve during extraction:

- `_(...)` i18n runs through every error string. Replace it with structured error
  data at the library boundary. `KeyProblem` in `gs1.py` already models this and
  is the template.
- `res.company` is the de-facto config store and token store. It needs a config
  and credential-store interface.
- Several response field names and error codes are assumed from live JSON rather
  than read from a spec. Extraction is the moment to pin them against
  `id.dev.epcis.cloud/q/openapi`. See `docs/api/observed-contract.md`.

## `benelog-client-python` API sketch

Signatures only. Illustrative, to be firmed in Phase 1.

```python
# core
@dataclass(frozen=True)
class ClientConfig:
    base_url: str
    request_timeout: tuple[int, int] = (5, 30)


class AuthStrategy(Protocol):
    def bearer(self) -> str: ...  # a valid access token


class TokenStore(Protocol):  # host persists rotated tokens
    def get_offline_token(self) -> str: ...
    def save_offline_token(self, token: str) -> None: ...
    def save_subject(self, username: str) -> None: ...


class OfflineTokenAuth(AuthStrategy):  # benelog credentials, mode 1
    def __init__(self, config, token_store, client_id, client_secret=""): ...


class Client:
    def __init__(self, config: ClientConfig, auth: AuthStrategy, session=None): ...
    def get(self, path, params=None): ...
    def put(self, path, payload): ...
    def post(self, path, payload=None): ...
    def delete(self, path): ...
    def post_file(self, path, filename, content, form=None): ...


# core.gs1 (already written, clean-room)
def check_digit(digits: str) -> str: ...
def digital_link(base_url: str, ai_values: dict[str, str]) -> str: ...


# masterdata
class PayloadBuilder:
    def place(self, payload: dict, gs1_path: str, value) -> None: ...


def upsert_product(client: Client, gtin: str, record: dict) -> None: ...
def upsert_organization(client: Client, gln: str, record: dict) -> None: ...


# registry
def draw_key(client: Client, ai: str) -> str: ...  # reserve
def confirm_key(client: Client, ai: str, key: str) -> None: ...  # commit
def release_key(client: Client, ai: str, key: str) -> None: ...
```

## Repository layout

Recommendation pending the answer to open question 4. Two options:

- Separate repositories, one per library. Cleanest licence boundary, an auditor
  sees one LICENSE per repo. Independent release cadence. More CI to run.
- Monorepo with per-package licensing. One place to change the contract and both
  libraries together. Harder to make the Apache-2.0 boundary obvious to an
  auditor, since the repo also holds host adapters under other licences.

Given the Apache-2.0 hygiene requirement and the audit rule that a reviewer must
see which licence applies to which file, separate repositories are the safer
default. Decide in Phase 0 close-out.

## Open questions

Ordered by how much they block Phase 1.

1. Resolver linkset and upstream Verified by GS1 are platform side today and
   absent from the addon. Do the libraries take these patterns client side, as the
   target lists, or do they stay a platform concern? This decides whether
   `resolver` and `registry` upstream are greenfield work or dropped.
2. Customer-token pass-through for GS1 Germany. Build it as the second auth mode,
   or leave it platform mediated? This blocks the `AuthStrategy` design.
3. Vocabulary manifest. Is there a benelog endpoint serving term status and
   aliases, or do we start with a pinned local manifest file? And does the manifest
   belong client side at all, given the JSON-LD framing is server side and the
   client speaks term-local JSON today?
4. Repository layout. Separate repositories or monorepo, and a benelog-owned repo
   for `benelog-client-python` separate from the Odoo addon.
5. Token storage, if customer-token arrives. Not plain `ir.config_parameter`.
   Propose an approach for review.
6. Odoo 19.0. Port the RFC 9728 discovery change (PR #1) now, or after it merges.
