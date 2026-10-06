import NextAuth from "next-auth";
import Cognito from "next-auth/providers/cognito";

/**
 * Auth.js v5 + Cognito hosted UI (foundation spec R4).
 * - Authorization-code flow against the user pool (confidential client).
 * - Access token kept in the (encrypted) session JWT; refreshed transparently.
 */

declare module "next-auth" {
  interface Session {
    accessToken?: string;
    tokenError?: string;
  }
}

async function refreshAccessToken(token: Record<string, unknown>) {
  try {
    const basic = Buffer.from(
      `${process.env.AUTH_COGNITO_ID}:${process.env.AUTH_COGNITO_SECRET}`
    ).toString("base64");
    const response = await fetch(`${process.env.COGNITO_DOMAIN}/oauth2/token`, {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        authorization: `Basic ${basic}`,
      },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        refresh_token: token.refreshToken as string,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw data;
    return {
      ...token,
      accessToken: data.access_token,
      expiresAt: Math.floor(Date.now() / 1000) + (data.expires_in ?? 3600),
      error: undefined,
    };
  } catch {
    return { ...token, error: "RefreshTokenError" };
  }
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: [
    Cognito({
      clientId: process.env.AUTH_COGNITO_ID,
      clientSecret: process.env.AUTH_COGNITO_SECRET,
      issuer: process.env.AUTH_COGNITO_ISSUER,
      authorization: {
        params: {
          // S14-02: self-service TOTP enrollment calls user-context Cognito
          // APIs, which require this scope ON THE ISSUED TOKEN — allowing it
          // on the app client is not enough, it must be requested here.
          // Scoped to the token holder's own account.
          scope: "openid email profile aws.cognito.signin.user.admin",
        },
      },
    }),
  ],
  // Pre-Beta hardening: 12h session (was the ~30d Auth.js default — too long
  // for a beta cookie), and sign-out REVOKES the Cognito refresh token so a
  // stolen cookie cannot mint new access tokens after logout. Access tokens
  // already issued stay valid ≤1h by design (stateless JWT validation).
  session: { strategy: "jwt", maxAge: 12 * 60 * 60 },
  pages: { signIn: "/" },
  events: {
    async signOut(message) {
      const token = "token" in message ? message.token : null;
      const refreshToken = token?.refreshToken as string | undefined;
      if (!refreshToken) return;
      try {
        const basic = Buffer.from(
          `${process.env.AUTH_COGNITO_ID}:${process.env.AUTH_COGNITO_SECRET}`
        ).toString("base64");
        await fetch(`${process.env.COGNITO_DOMAIN}/oauth2/revoke`, {
          method: "POST",
          headers: {
            "content-type": "application/x-www-form-urlencoded",
            authorization: `Basic ${basic}`,
          },
          body: new URLSearchParams({ token: refreshToken }),
        });
      } catch {
        // best-effort: revocation failure must not block sign-out
      }
    },
  },
  callbacks: {
    // NOTE: no `authorized` callback — the middleware wrapper would enforce it
    // on EVERY wrapped request (including /api/auth/*); route gating lives in
    // middleware.ts with explicit path checks instead.
    async jwt({ token, account, profile }) {
      if (account) {
        return {
          ...token,
          accessToken: account.access_token,
          refreshToken: account.refresh_token,
          expiresAt: account.expires_at,
          email: (profile?.email as string) ?? token.email,
        };
      }
      const expiresAt = (token.expiresAt as number | undefined) ?? 0;
      if (Date.now() / 1000 < expiresAt - 60) return token;
      return refreshAccessToken(token);
    },
    session({ session, token }) {
      session.accessToken = token.accessToken as string | undefined;
      session.tokenError = token.error as string | undefined;
      if (token.email && session.user) session.user.email = token.email as string;
      return session;
    },
  },
});
