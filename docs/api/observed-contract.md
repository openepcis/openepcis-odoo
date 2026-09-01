<!-- SPDX-License-Identifier: LGPL-3.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG -->

# Observed API contract

Phase 0 output. This reconstructs the platform API from the addon's own calls,
per interaction pattern, with each call site cited. It marks what is verified
against a published spec versus what the addon assumes from live JSON.

The resolver publishes live OpenAPI 3.1 at `id.dev.epcis.cloud/q/openapi` (75
paths). "Verified" below means the path and verb appear there. Payload field
names and error codes are marked "assumed" where the addon reads them from live
responses rather than from a schema. `tools/check-contract.py` already diffs a
curated subset against that live spec.

All calls go through `openepcis.client` verbs. Base URL from
`res.company.openepcis_base_url`.

## Auth and discovery (core transport)

| Step | Call | Source |
|---|---|---|
| Protected resource metadata (RFC 9728) | `GET {base}/.well-known/oauth-protected-resource` to `authorization_servers[0]` | `openepcis_client.py:176-210` |
| OIDC discovery | `GET {issuer}/.well-known/openid-configuration` to `token_endpoint` | `openepcis_client.py:215-252` |
| Token exchange | `POST {token_endpoint}` grant `refresh_token` | `openepcis_client.py:279-321` |

One auth mode is implemented: benelog credentials via an OIDC offline token.
Keycloak rotates the refresh token when "Revoke Refresh Token" is on; the client
stores the rotated token. Customer-token pass-through for GS1 Germany is not
implemented.

Behaviour the connector guarantees, which no per-endpoint OpenAPI states:

- Idempotent verbs (GET, PUT, DELETE, HEAD) retry up to three times with backoff.
  POST does not retry.
- A 401 triggers exactly one silent re-auth, which does not consume a retry.

## registry: key allocation

Verified paths (present in `/q/openapi`).

| Call | Verb | Body / params | Response | Source |
|---|---|---|---|---|
| Draw a key (reserve) | POST | `/gs1de/keys/draw` body `{"ai": "01" \| "414" \| "417"}` | `{"key": "..."}` | `openepcis_key_pool.py:81` |
| Release a candidate | DELETE | `/gs1de/keys/{ai}/{key}` | 204 | `openepcis_key_pool.py:117` |
| Confirm after publish (commit) | POST | `/gs1de/keys/{ai}/{key}/confirm` | 200 | `openepcis_key_pool.py:137` |
| Diagnostic probe | GET | `/gs1de/keys?ai=01` | list | `openepcis_client.py:645` |

Draw is non-idempotent, burns a key, single attempt, timeout `(5, 90)`.

Assumed, not read from a spec:

- The response carries a `key` field.
- Error semantics: 409 means no licence, 403 means not allowed, 400 with the
  substring "carries no" means a missing claim. Inferred from status and body
  substring (`openepcis_key_pool.py:164-179`, `exceptions.py:46`).

## masterdata: upsert and bulk

Verified paths.

| Call | Verb | Body | Response | Source |
|---|---|---|---|---|
| Upsert product | PUT | `/products/{gtin}`, mapping payload + `gtin` | 200 | `openepcis_sync_mixin.py:333`, endpoint `product_product.py:39` |
| Upsert organization | PUT | `/organizations/{gln}`, mapping payload + `globalLocationNumber` | 200 | `openepcis_sync_mixin.py:333`, endpoint `res_partner.py:89` |
| Bulk products | POST | `/bulk/products` multipart CSV, `form={"format":"csv"}` | `{total, successCount, errorCount, errors:[{rowNumber,errorCode,errorMessage}]}` | `openepcis_bulk_import.py:159` |
| Bulk organizations | POST | `/bulk/organizations` multipart CSV | same shape | `openepcis_bulk_import.py:176` |
| Diagnostic probe | GET | `/products?page=0&pageSize=1` | page | `openepcis_client.py:636` |

PUT is create-or-update with server-side merge, so it is idempotent.

Assumed, not read from a spec:

- Bulk size cap of 10 MB, and error codes `DUPLICATE_GTIN` / `DUPLICATE_GLN`,
  declared in the wizard docstring (`openepcis_bulk_import.py:12-19`, matched at
  206 and 240).
- The bulk CSV column vocabulary, which diverges from the per-record term set (see
  the vocabulary inventory in the roadmap).

## masterdata / registry: read-only helpers

| Call | Verb | Response | Source |
|---|---|---|---|
| Channels | GET | `/sync/channels` to `{id, displayName, enabled, dryRun, configured, requiredTerms:{KIND:[gs1:term]}}` | `openepcis_channel.py:61-77` |
| GPC search | GET | `/gpc/search?q=&level=BRICK&size=40` to `[{code, title, definition, lineage \| path}]` | `openepcis_gpc_search.py:53-68` |

Assumed: the channel field names (`displayName`, `dryRun`, `configured`,
`requiredTerms`) and the GPC result keys. Read from live JSON, not a schema.

## resolver: linkset

Not implemented. The addon builds a Digital Link locally (`gs1.digital_link`,
`openepcis_sync_mixin.py:118`) and renders it as a URL and QR code. There is no
PATCH or PUT to any linkset endpoint. This pattern is greenfield.

When built, it must be a partial update, not a full replace, and must carry
per-link ownership so the connector does not delete links it does not own. If the
API lacks ownership attribution, that is a required API change, not a client
workaround.

## registry: upstream and Verified by GS1

No direct call. The addon reads channel requirements only. Onward publication to
the GS1 Germany Service Platform, including Verified by GS1, happens on the
platform side. Customer-credential delegation is absent. This pattern is
greenfield, and depends on the customer-token auth mode.

## Notes for extraction

- Pin every "assumed" row against `id.dev.epcis.cloud/q/openapi` during Phase 1.
  Where the live spec and the addon disagree, the spec wins and the client changes.
- The error mapping that keys on the body substring "carries no" is fragile.
  Replace it with the RFC 7807 `type` or a documented error code once the spec is
  read.
