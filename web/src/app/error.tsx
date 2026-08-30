"use client";
/**
 * 全站 route segment 錯誤頁。⭐ 使用者裁決（2026-08-30）：任何 runtime 錯誤都
 * 不得把 stack、錯誤訊息或程式碼曝露給使用者——本頁只渲染 copy.ts 的固定文案。
 * （next dev 的紅色 overlay 是開發模式專屬，production build 不會出現；本頁
 * 涵蓋的是 production 下 segment render 拋錯的情況。）
 * 錯誤本體只進 console（供營運在瀏覽器 devtools 取證），不進 DOM。
 */
import Link from "next/link";
import { useEffect } from "react";
import { useCopy } from "@/lib/lang";

export default function ErrorPage({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.errorPage;
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <main className="error-page">
      <div className="card error-page-card">
        <h1 className="error-page-title">{c.title}</h1>
        <p className="error-page-desc">{c.desc}</p>
        <div className="error-page-actions">
          <button type="button" className="btn btn-primary" onClick={() => reset()}>
            {c.retry}
          </button>
          <Link className="btn" href="/">
            {c.home}
          </Link>
        </div>
      </div>
    </main>
  );
}
