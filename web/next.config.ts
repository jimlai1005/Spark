import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    // dev：同源反代到本機 FastAPI（cookie 認證免 CORS）。
    // port 8700 對齊 scripts/run_api.py 的 FILET_API_PORT 預設值（非計畫文件原文的 8000）。
    // prod：nginx 直接路由 /api，不會打到這條（部署計畫）。
    return [
      {
        source: "/api/:path*",
        destination: "http://127.0.0.1:8700/api/:path*",
      },
    ];
  },
};

export default nextConfig;
