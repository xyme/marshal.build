# marshal frontend — Next.js standalone build (CodeBuild, repo root as context):
#   docker build -f frontend/Dockerfile \
#     --build-arg NEXT_PUBLIC_COGNITO_DOMAIN=... \
#     --build-arg NEXT_PUBLIC_COGNITO_CLIENT_ID=... .
# ECR Public base image: Docker Hub anonymous pulls from AWS IPs hit 429 rate limits
FROM public.ecr.aws/docker/library/node:22-alpine AS builder
WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./

# NEXT_PUBLIC_* values are inlined into the client bundle at build time
ARG NEXT_PUBLIC_COGNITO_DOMAIN
ARG NEXT_PUBLIC_COGNITO_CLIENT_ID
ARG NEXT_PUBLIC_SUPPORT_EMAIL
ARG NEXT_PUBLIC_SUPPORT_HOURS
ENV NEXT_PUBLIC_SUPPORT_EMAIL=$NEXT_PUBLIC_SUPPORT_EMAIL \
    NEXT_PUBLIC_SUPPORT_HOURS=$NEXT_PUBLIC_SUPPORT_HOURS
ENV NEXT_PUBLIC_COGNITO_DOMAIN=$NEXT_PUBLIC_COGNITO_DOMAIN \
    NEXT_PUBLIC_COGNITO_CLIENT_ID=$NEXT_PUBLIC_COGNITO_CLIENT_ID \
    NEXT_TELEMETRY_DISABLED=1

# Build-time placeholders for server-only auth env (validated at runtime, not build)
RUN AUTH_SECRET=build-placeholder \
    AUTH_COGNITO_ID=${NEXT_PUBLIC_COGNITO_CLIENT_ID:-build} \
    AUTH_COGNITO_SECRET=build-placeholder \
    AUTH_COGNITO_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_build \
    npm run build

FROM public.ecr.aws/docker/library/node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -S nextjs && adduser -S nextjs -G nextjs
COPY --from=builder --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nextjs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nextjs /app/public ./public

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
