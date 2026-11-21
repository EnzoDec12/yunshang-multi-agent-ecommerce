import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: "standalone",
  async rewrites() {
    const internalGateway = process.env.GATEWAY_INTERNAL_URL?.replace(/\/$/, "");
    if (!internalGateway) {
      return [];
    }
    return [
      {
        source: "/gateway-proxy/:path*",
        destination: `${internalGateway}/:path*`,
      },
    ];
  },
};

export default nextConfig;
