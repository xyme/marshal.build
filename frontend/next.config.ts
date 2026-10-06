import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output for the slim ECS container image (frontend/Dockerfile)
  output: "standalone",
  // Pin the file-tracing root to THIS directory. Without it, Next infers the
  // root from the nearest parent lockfile — so adding a repo-root package.json
  // (S15-06 added one for the playwright/axe drill deps) silently nested the
  // standalone output under `.next/standalone/frontend/`, which would break
  // `CMD ["node", "server.js"]` for anyone building outside the Docker context.
  outputFileTracingRoot: __dirname,
  // S14-03: the only next/image uses are one static local logo we ship
  // ourselves, so runtime optimization buys nothing and keeps sharp (and its
  // inherited libvips CVEs) on the serving path. Disabling it removes that
  // surface entirely rather than suppressing the finding.
  images: { unoptimized: true },
  // S15-04: import documentation markdown as strings so /docs is static and
  // versioned with the build (no docs service, no runtime fetch).
  turbopack: {
    rules: {
      "*.md": { loaders: ["raw-loader"], as: "*.js" },
    },
  },
  webpack: (config) => {
    config.module.rules.push({ resourceQuery: /raw/, type: "asset/source" });
    config.module.rules.push({ test: /\.md$/, type: "asset/source" });
    return config;
  },
};

export default nextConfig;
