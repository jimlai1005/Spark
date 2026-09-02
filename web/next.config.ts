import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async redirects() {
    // 2026-09-02（上線前回歸裁決 B）：`/leaderboard` 自 M3 round3 起由 `/explore` 取代。
    // 原本是 client-side `router.replace`（不執行 JS 的爬蟲／curl 拿到 200＋空殼頁），
    // 改成伺服器層永久轉址：語意乾淨、舊書籤與搜尋收錄正式導向新址。
    // permanent=true → Next 回 308（保留 method）；regression_check 接受 301/302/307/308。
    return [{ source: "/leaderboard", destination: "/explore", permanent: true }];
  },
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
