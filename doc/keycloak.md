# Keycloak: what the offline token needs

Measured against **Keycloak 26.2.5** — the version the platform runs — not derived
from documentation. `keycloak-test-realm.json` in this directory is the realm that
produced these results and can be imported to reproduce them:

```bash
podman run -d --name kc-test -p 8180:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  -v ./doc/keycloak-test-realm.json:/opt/keycloak/data/import/realm.json:ro \
  quay.io/keycloak/keycloak:26.2.5 start-dev --import-realm
```

It is a throwaway: the passwords and secrets in it are literals, and direct access
grants are on so a token can be minted without a browser. **Neither belongs in a
real realm** — see below.

## The client

| Setting | Value | Why |
|---|---|---|
| Client | confidential | The connector can keep a secret; a public client would let anyone holding the token id use it |
| `offline_access` | allowed | Without it Keycloak issues an ordinary refresh token that stops working when the session ends |
| Direct access grants | **off** | The password grant is gone in OAuth 2.1. On in the test realm only, to mint without a browser |
| Standard token exchange | see below | Not needed for issuing an offline token |

## The claims the resolver insists on

Three things must reach the access token, or the resolver refuses the call:

| Claim / role | Missing means |
|---|---|
| Realm role named after the tenant (`acme`, plus `acme/create`, `acme/update`) | `403` on every write |
| `defaultGroup` | `400 … carries no defaultGroup claim` |
| `gs1CompanyPrefix` | `403` when drawing identifiers |

Verified present in a minted access token:

```
defaultGroup      : acme
gs1CompanyPrefix  : 9520000
realm_access.roles: ['acme', 'acme/create', 'acme/update', 'dpp-writer', 'offline_access']
```

**Trap when writing a realm import.** Declaring `clientScopes` in the realm JSON
*replaces* Keycloak's built-in scopes rather than adding to them. The `roles` scope
then no longer exists, its realm-roles mapper never runs, and `realm_access.roles`
comes out **empty** — while `defaultGroup` and `gs1CompanyPrefix` look fine, so the
symptom is a bare `403` with no obvious cause. This realm therefore attaches the two
claim mappers directly to the client and leaves the built-in scopes alone.

## Token exchange (RFC 8693) cannot mint an offline token

Worth stating plainly, because it was the intended design:

- The realm advertises `urn:ietf:params:oauth:grant-type:token-exchange`.
- **Standard token exchange (v2, GA in 26.2.5) refuses
  `requested_token_type=urn:ietf:params:oauth:token-type:refresh_token`** with
  `invalid_request — requested_token_type unsupported`. Measured twice, with and
  without an explicit audience.
- It also requires the exchanging client to be inside the subject token's audience
  (`access_denied — Client is not within the token audience`) and the requested
  audience to be resolvable (`invalid_request — Requested audience not available`).
  Both are solvable with audience mappers; the refresh-token limit is not.
- Legacy `token-exchange:v1` did support requesting refresh tokens, but answers
  `access_denied — Client not allowed to exchange` until fine-grained admin
  authorization is enabled and per-client exchange permissions are configured. It
  is a deprecated preview feature.

**So an offline token is issued the ordinary way: an authorization-code flow with
`scope=offline_access` and `prompt=consent` against the connector client.** A human
is present, consents, and the resulting token is bound to that client — which is
also what makes per-connector revocation possible. Nothing here needs a preview
feature.

## A token is bound to its issuer URL

Refreshing an offline token through a *different* hostname for the same realm fails
with `invalid_grant — Invalid token issuer. Expected '…'`. This is a live risk
wherever an alias and a canonical name both resolve — the platform has
`auth.…` and `keycloak.…` serving one realm, with `auth.…` as the canonical issuer.

The connector reports this case specifically rather than as "revoked", because the
two need entirely different responses.

## Revocation

Removing the user's offline sessions (admin API `POST /users/{id}/logout`) makes the
next refresh fail with `invalid_grant — Stale token`. The connector reports it as
revoked and asks for a fresh token, which is correct.

## What was verified end to end

Against the realm above, with the connector's own code and no stubs:

- an offline token is minted, `typ: Offline`, `refresh_expires_in: 0`
- the connector discovers the token endpoint from the realm URL
- it exchanges the offline token for a 120-second access token carrying all three
  required claims
- the access token is cached and re-minted on demand
- **Keycloak rotates the offline token on every refresh, and the connector stores
  the new one** — proven to survive across separate processes, which is the
  lock-out scenario
- a revoked token produces a message naming the cause
