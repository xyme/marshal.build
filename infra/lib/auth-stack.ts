import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { Construct } from "constructs";

/**
 * Cognito user pool per foundation spec R3/R4.
 *
 * S15-01 (owner decision D1): enterprise SSO federates BOTH Entra ID (SAML)
 * and Okta (OIDC) into this same pool. Providers are config-gated so synth
 * and dev deploys work before the owner provides dev-tenant metadata:
 *   - Entra:  ENTRA_SAML_METADATA_URL   (federation metadata document URL)
 *   - Okta:   OKTA_OIDC_ISSUER          (client id/secret read at DEPLOY time
 *             from Secrets Manager `marshal/sso/okta` via CFN dynamic
 *             references — never stored in the template)
 * The native COGNITO provider is ALWAYS retained on the client: that is the
 * break-glass admin path (docs/sso.md). Role/persona mapping from IdP claims
 * is done by the pre-token-generation trigger below, not trusted blindly:
 * only recognized role values grant groups.
 */
export class MarshalAuthStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Single gate for every federation-related resource. With neither IdP env
    // configured, this stack synthesizes EXACTLY as it did pre-S15-01: no
    // providers, no pool-schema change, no pre-token trigger. SSO is built and
    // reviewable in code, and inert until the owner supplies dev-tenant config
    // (docs/sso.md). Verified with `cdk diff MarshalAuthStack` → no changes.
    const entraMetadataUrl = process.env.ENTRA_SAML_METADATA_URL;
    const oktaIssuer = process.env.OKTA_OIDC_ISSUER;
    const ssoEnabled = Boolean(entraMetadataUrl || oktaIssuer);

    const userPool = new cognito.UserPool(this, "UserPool", {
      userPoolName: "marshal-ai-users",
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
        fullname: { required: false, mutable: true },
      },
      // S15-01/S15-02: landing pads for IdP claim mapping, created ONLY when an
      // IdP is configured (see ssoEnabled below). `idp_roles` receives the IdP's
      // role/group claim (mapped to platform groups by the pre-token trigger);
      // `tenant` is the org-boundary claim reserved for GA multi-org (D12 keeps
      // Alpha 4 single-org, so it maps but resolves to the platform tenant).
      // Schema changes on a LIVE pool are add-only and irreversible, so they
      // stay out of the template entirely while SSO is off — a no-SSO deploy
      // must not touch the pool that holds real users.
      ...(ssoEnabled
        ? {
            customAttributes: {
              idp_roles: new cognito.StringAttribute({ mutable: true, maxLen: 2048 }),
              tenant: new cognito.StringAttribute({ mutable: true, maxLen: 64 }),
            },
          }
        : {}),
      passwordPolicy: {
        minLength: 12,
        requireDigits: true,
        requireLowercase: true,
        requireUppercase: true,
        requireSymbols: false,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      // S14-02 (owner decision D8): TOTP available to everyone, REQUIRED for
      // admins. Cognito cannot condition MFA on group membership, so the pool
      // is OPTIONAL and the admin requirement is enforced platform-side
      // (app/core/auth.py checks the token's amr claim on admin routes).
      // SMS is deliberately not enabled: no SMS origination identity, and
      // SIM-swap risk makes it the weaker second factor anyway.
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { otp: true, sms: false },
      deletionProtection: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // Account-scoped default keeps the globally-unique domain prefix collision-free
    // across accounts (resolves at deploy; stays synthesizable without credentials).
    const domainPrefix = process.env.COGNITO_DOMAIN_PREFIX ?? `marshal-ai-${this.account}`;
    const domain = userPool.addDomain("HostedUi", {
      cognitoDomain: { domainPrefix },
    });

    // ------------------------------------------------------------ S15-01 SSO
    // Pre-token-generation trigger: maps the IdP's role claim (landed on
    // custom:idp_roles by the provider attribute mapping) onto platform
    // groups, so the backend's existing cognito:groups resolution works
    // unchanged for federated users. Security posture:
    //   - only RECOGNIZED role values grant groups (an IdP sending
    //     "marshal-admins,garbage" grants admin, ignores garbage — an IdP
    //     cannot invent arbitrary pool groups);
    //   - the override is the UNION of native pool groups and mapped roles: a
    //     native admin grant is never lowered by federation (mirrors the
    //     teams "highest wins" philosophy);
    //   - custom:tenant passes through to the ID token only (access-token
    //     custom claims need pre-token-gen V2 — a GA multi-org concern; D12
    //     keeps Alpha 4 single-org where the platform-tenant fallback holds).
    const roleMapping =
      process.env.IDP_ROLE_MAPPING ??
      '{"marshal-admins":"admin","marshal-power":"power","marshal-business":"business"}';
    const preTokenFn = ssoEnabled ? new lambda.Function(this, "PreTokenGeneration", {
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: "index.handler",
      environment: { ROLE_MAPPING: roleMapping },
      code: lambda.Code.fromInline(`
const MAPPING = JSON.parse(process.env.ROLE_MAPPING || "{}");
exports.handler = async (event) => {
  const attrs = event.request.userAttributes || {};
  const native = (event.request.groupConfiguration || {}).groupsToOverride || [];
  const raw = attrs["custom:idp_roles"] || "";
  const mapped = raw
    .split(/[,;\\s\\[\\]"]+/)
    .map((value) => MAPPING[value])
    .filter(Boolean);
  const claims = {};
  if (attrs["custom:tenant"]) claims["custom:tenant"] = attrs["custom:tenant"];
  if (mapped.length === 0 && Object.keys(claims).length === 0) return event;
  event.response = event.response || {};
  event.response.claimsOverrideDetails = {
    ...(Object.keys(claims).length ? { claimsToAddOrOverride: claims } : {}),
    ...(mapped.length
      ? { groupOverrideDetails: { groupsToOverride: [...new Set([...native, ...mapped])] } }
      : {}),
  };
  return event;
};
`),
    }) : undefined;
    if (preTokenFn) {
      userPool.addTrigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION, preTokenFn);
    }

    // Entra ID via SAML 2.0 — gated on the federation metadata URL (owner
    // provides it from the dev tenant's enterprise application).
    let entraProvider: cognito.UserPoolIdentityProviderSaml | undefined;
    if (entraMetadataUrl) {
      entraProvider = new cognito.UserPoolIdentityProviderSaml(this, "EntraIdSaml", {
        userPool,
        name: "EntraID",
        metadata: cognito.UserPoolIdentityProviderSamlMetadata.url(entraMetadataUrl),
        attributeMapping: {
          email: cognito.ProviderAttribute.other(
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"
          ),
          fullname: cognito.ProviderAttribute.other(
            "http://schemas.microsoft.com/identity/claims/displayname"
          ),
          custom: {
            // Entra app roles claim → role mapping input (docs/sso.md)
            idp_roles: cognito.ProviderAttribute.other(
              "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"
            ),
          },
        },
      });
    }

    // Okta via OIDC — gated on the issuer; client id/secret resolve at deploy
    // from Secrets Manager `marshal/sso/okta` (fields: client_id,
    // client_secret) through CFN dynamic references — never in the template.
    let oktaProvider: cognito.UserPoolIdentityProviderOidc | undefined;
    if (oktaIssuer) {
      oktaProvider = new cognito.UserPoolIdentityProviderOidc(this, "OktaOidc", {
        userPool,
        name: "Okta",
        issuerUrl: oktaIssuer,
        clientId: cdk.SecretValue.secretsManager("marshal/sso/okta", {
          jsonField: "client_id",
        }).unsafeUnwrap(),
        clientSecret: cdk.SecretValue.secretsManager("marshal/sso/okta", {
          jsonField: "client_secret",
        }).unsafeUnwrap(),
        scopes: ["openid", "email", "profile", "groups"],
        attributeRequestMethod: cognito.OidcAttributeRequestMethod.GET,
        attributeMapping: {
          email: cognito.ProviderAttribute.other("email"),
          fullname: cognito.ProviderAttribute.other("name"),
          custom: {
            // Okta groups claim (custom auth-server claim; docs/sso.md)
            idp_roles: cognito.ProviderAttribute.other("groups"),
          },
        },
      });
    }

    const callbackUrls = (process.env.OAUTH_CALLBACK_URLS ?? "http://localhost:3000/api/auth/callback/cognito").split(",");
    const logoutUrls = (process.env.OAUTH_LOGOUT_URLS ?? "http://localhost:3000").split(",");

    const client = userPool.addClient("WebClient", {
      // Break-glass invariant (S15-01): COGNITO native sign-in stays enabled
      // no matter which IdPs exist — admin lockout via a broken IdP must
      // always have a recovery path (docs/sso.md). Left UNSET while SSO is off
      // so the property does not appear in the template at all (CFN's implicit
      // default is COGNITO-only, which is exactly the pre-S15 behavior).
      supportedIdentityProviders: ssoEnabled
        ? [
            cognito.UserPoolClientIdentityProvider.COGNITO,
            ...(entraProvider ? [cognito.UserPoolClientIdentityProvider.custom("EntraID")] : []),
            ...(oktaProvider ? [cognito.UserPoolClientIdentityProvider.custom("Okta")] : []),
          ]
        : undefined,
      userPoolClientName: "marshal-web",
      generateSecret: true,
      authFlows: {
        userSrp: true,
        // Enables scripted token retrieval for seeding/smoke tests (SECRET_HASH required)
        userPassword: true,
      },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
          // S14-02: required for the user-context Cognito APIs behind
          // self-service TOTP enrollment (GetUser, AssociateSoftwareToken,
          // VerifySoftwareToken, SetUserMFAPreference). Scope is limited to
          // the token holder's OWN account; the token never reaches the
          // browser (the SSR proxy holds it in an encrypted session).
          cognito.OAuthScope.COGNITO_ADMIN,
        ],
        callbackUrls,
        logoutUrls,
      },
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
      preventUserExistenceErrors: true,
    });

    for (const [name, description] of [
      ["business", "Business users — guided experience"],
      ["power", "Power users — full spec/deploy control"],
      ["admin", "Platform administrators"],
    ] as const) {
      new cognito.CfnUserPoolGroup(this, `Group-${name}`, {
        userPoolId: userPool.userPoolId,
        groupName: name,
        description,
      });
    }

    new cdk.CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new cdk.CfnOutput(this, "UserPoolClientId", { value: client.userPoolClientId });
    new cdk.CfnOutput(this, "HostedUiDomain", {
      value: `https://${domain.domainName}.auth.${this.region}.amazoncognito.com`,
    });
    // The client references providers by name — enforce creation order.
    if (entraProvider) client.node.addDependency(entraProvider);
    if (oktaProvider) client.node.addDependency(oktaProvider);

    new cdk.CfnOutput(this, "CognitoIssuer", {
      value: `https://cognito-idp.${this.region}.amazonaws.com/${userPool.userPoolId}`,
    });
    if (ssoEnabled) {
      new cdk.CfnOutput(this, "FederatedIdps", {
        value: [entraProvider && "EntraID(SAML)", oktaProvider && "Okta(OIDC)"]
          .filter(Boolean)
          .join(","),
      });
    }
  }
}
