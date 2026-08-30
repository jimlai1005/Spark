"use client";
/**
 * root layout 層級的錯誤頁（layout 本身拋錯時 error.tsx 蓋不到，Next 改渲染
 * 本元件並**取代整個 root layout**）。因此：(1) 必須自帶 <html><body>；
 * (2) Providers／LangProvider 不存在，不能用 useCopy——直接 import COPY_ZH
 * （單一文案來源不變，預設繁中）。同 error.tsx 的使用者裁決：絕不曝露
 * stack／錯誤訊息／程式碼。
 */
import { COPY_ZH } from "@/lib/copy";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const c = COPY_ZH.errorPage;
  console.error(error);
  return (
    <html lang="zh-Hant">
      <body style={{ background: "#07080a", color: "#e9ecef", fontFamily: "'Noto Sans TC', sans-serif" }}>
        <main style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
          <div style={{ maxWidth: 420, textAlign: "center" }}>
            <h1 style={{ fontSize: 22, marginBottom: 12 }}>{c.title}</h1>
            <p style={{ color: "#868f99", lineHeight: 1.7, marginBottom: 24 }}>{c.desc}</p>
            <button
              type="button"
              onClick={() => reset()}
              style={{
                background: "#46d6b3",
                color: "#07080a",
                border: 0,
                borderRadius: 9,
                padding: "10px 22px",
                fontSize: 15,
                cursor: "pointer",
              }}
            >
              {c.retry}
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
