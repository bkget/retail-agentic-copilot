/**
 * The browser talks only to this Next.js origin; /api/* is proxied server-side to the
 * backend. One origin means no CORS, the API port needn't be exposed publicly, and
 * cookies/auth added later just work. BACKEND_INTERNAL_URL is resolved at build time
 * (Docker: the compose service name; local `npm run dev`: localhost).
 *
 * compress:false matters for SSE: gzip buffers the stream, which would make the
 * "thinking" steps and typed-out answer arrive all at once at the end.
 */
const backend = process.env.BACKEND_INTERNAL_URL ?? "http://localhost:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  compress: false,
  poweredByHeader: false,
  experimental: {
    proxyTimeout: 120_000,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Frame-Options", value: "DENY" },
        ],
      },
    ];
  },
};

export default nextConfig;
