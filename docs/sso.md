# Enterprise SSO federation (S15-01, owner decision D1)

**Status (29 Jul 2026): built config-gated, drill PENDING owner credentials.**
Both federation paths (Entra ID via SAML 2.0, Okta via OIDC) are implemented
in `infra/lib/auth-stack.ts` and synth-verified. The live drill requires
owner-provided dev tenants; no `marshal/sso/*` secrets exist in the account
yet (verified 29 Jul). Until then the platform runs native Cognito sign-in
unchanged.

## Architecture in one paragraph

External IdPs federate INTO the existing Cognito user pool — the hosted-UI
flow, the web client, and the backend JWT validation are unchanged. Each
provider maps its role/group claim onto the pool attribute `custom:idp_roles`;
a pre-token-generation Lambda translates recognized values into the existing
platform groups (`admin`/`power`/`business`) as a group override, so
`app/core/auth.py` resolves roles for federated users through exactly the
same `cognito:groups` path as native users. First federated sign-in
JIT-provisions the user row and writes a `user_federated_signin` SECURITY
audit event.

## Claim mapping (the documented mapping the AC requires)

### Entra ID (SAML 2.0)
| IdP claim | Pool attribute | Notes |
|-----------|----------------|-------|
| `.../ws/2005/05/identity/claims/emailaddress` | `email` | required |
| `.../identity/claims/displayname` | `name` | display name |
| `.../ws/2008/06/identity/claims/role` | `custom:idp_roles` | **App roles**, not security groups — define app roles named per the role table below on the enterprise application |

### Okta (OIDC)
| IdP claim | Pool attribute | Notes |
|-----------|----------------|-------|
| `email` | `email` | required |
| `name` | `name` | display name |
| `groups` | `custom:idp_roles` | add a `groups` claim to the authorization server, filtered to `marshal-*` |

### Role mapping (pre-token Lambda, `IDP_ROLE_MAPPING` env)
| IdP role/group value | Platform role |
|----------------------|---------------|
| `marshal-admins` | `admin` |
| `marshal-power` | `power` |
| `marshal-business` | `business` |

Rules enforced by the Lambda:
- **Only recognized values grant groups** — an IdP asserting anything else
  grants nothing (an IdP cannot invent platform groups).
- **Union semantics** — native pool group grants are never lowered by
  federation (mirrors the platform's "highest wins, never lowered" access
  philosophy).
- Persona: federated users with `power`/`admin` land as Power persona;
  business users go through onboarding persona selection, same as native.
- `custom:tenant` maps and passes to the ID token; per D12 (single org at
  Alpha 4) everyone resolves to the platform tenant. Access-token tenant
  claims are a GA multi-org item (needs pre-token-generation V2).

## Configuration contract (deploy-time)

| Input | Where | Used for |
|-------|-------|----------|
| `ENTRA_SAML_METADATA_URL` | env at `cdk deploy MarshalAuthStack` | federation metadata of the Entra enterprise app |
| `OKTA_OIDC_ISSUER` | env at deploy | e.g. `https://<org>.okta.com/oauth2/default` |
| `marshal/sso/okta` secret | Secrets Manager, fields `client_id`, `client_secret` | resolved via CFN dynamic reference — never in the template |
| `IDP_ROLE_MAPPING` | env at deploy (optional) | overrides the role table above |

IdP-side redirect/entity IDs (from the auth stack outputs):
- SAML ACS URL: `https://<HostedUiDomain>/saml2/idpresponse`
- SAML audience/entity ID: `urn:amazon:cognito:sp:<UserPoolId>`
- OIDC sign-in redirect: `https://<HostedUiDomain>/oauth2/idpresponse`

## Break-glass native admin (retained by construction)

The web client's `SupportedIdentityProviders` ALWAYS includes `COGNITO` —
verified in the synthesized template. If an IdP misconfiguration locks out
federated admins: sign in with a native admin at the hosted UI directly
(admin MFA requirement, S14-02, still applies), fix or detach the provider,
redeploy the auth stack. Native admin credentials live outside the IdP on
purpose; do not "clean up" the native admin accounts.

## Drill runbook (run when owner provides dev tenants)

1. Owner: Entra dev tenant → enterprise application (SAML) with the three app
   roles; Okta dev org → OIDC web app + `groups` claim. Put the Okta
   client id/secret in `marshal/sso/okta`; export both env vars.
2. `cdk deploy MarshalAuthStack` with the envs → `FederatedIdps` output lists
   both.
3. Drill per IdP: fresh test user in each of the three roles → hosted UI now
   shows the IdP buttons → sign in → verify: JIT row created with the mapped
   role (`/admin/users`), `user_federated_signin` audit event, group override
   visible in the access token, hosted-UI native sign-in still works.
4. Negative: IdP user with NO marshal role → lands as `business` (default),
   nothing else granted. IdP asserting an unknown role value → ignored.
5. Offboarding tie-in (S15-03): disable the user IdP-side, then offboard
   platform-side — verify session revocation.
6. Record results in §13 and flip the status line at the top of this doc.
