import type { NextConfig } from "next";

const DJANGO_ORIGIN = process.env.DJANGO_ORIGIN || "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  devIndicators: false,
  async rewrites() {
    return [
      { source: "/backend/login", destination: `${DJANGO_ORIGIN}/login` },
      { source: "/backend/signup", destination: `${DJANGO_ORIGIN}/signup` },
      { source: "/logout", destination: `${DJANGO_ORIGIN}/logout` },
      { source: "/session/end", destination: `${DJANGO_ORIGIN}/session/end` },
      { source: "/api/:path*", destination: `${DJANGO_ORIGIN}/api/:path*` },
      {
        source: "/runs/:runId/status.json",
        destination: `${DJANGO_ORIGIN}/runs/:runId/status.json`,
      },
      {
        source: "/runs/:runId/all-files.zip",
        destination: `${DJANGO_ORIGIN}/runs/:runId/all-files.zip`,
      },
      {
        source: "/runs/:runId/steps/:order/outputs.zip",
        destination: `${DJANGO_ORIGIN}/runs/:runId/steps/:order/outputs.zip`,
      },
      {
        source: "/runs/:runId/steps/:order/preview.svg",
        destination: `${DJANGO_ORIGIN}/runs/:runId/steps/:order/preview.svg`,
      },
      {
        source: "/runs/:runId/steps/:order/preview-source",
        destination: `${DJANGO_ORIGIN}/runs/:runId/steps/:order/preview-source`,
      },
      { source: "/static/:path*", destination: `${DJANGO_ORIGIN}/static/:path*` },
    ];
  },
};

export default nextConfig;
