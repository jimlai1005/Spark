"use client";
/**
 * `components/Toast.tsx` — 輕量 fixed-position 通知（M3 round3 Task 8，R2·P0）。
 *
 * 抽成獨立檔案而非直接寫在 `app/settings/page.tsx`（原派工 prompt「在 settings
 * 頁內做」）是因為 Next.js app router 對 `page.tsx` 的具名 export 有白名單限制
 * （`metadata`/`generateStaticParams`/… 之外一律型別錯誤，`tsc --noEmit` 會炸）——
 * 這是實作途中發現的框架限制，不是新功能；元件本身沒有超出 plan 描述的行為
 * （右下角、可手動關閉、8 秒自動消失、無第三方套件），只是換了檔案位置以滿足
 * Next 的路由檔案規則，同時讓元件可獨立單元測試（fake timers）。
 *
 * `message` 變了視為新的一則通知，重新起算 8 秒（見 effect deps）。
 */
import { useEffect } from "react";

export function Toast({
  message,
  onDismiss,
  dismissLabel,
}: {
  message: string;
  onDismiss: () => void;
  /** 關閉按鈕的 aria-label（文案來自呼叫端的 `copy.ts`，本元件不內嵌任何語言字串）。 */
  dismissLabel: string;
}) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 8000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [message]);

  return (
    <div
      role="alert"
      style={{
        position: "fixed", right: 20, bottom: 20, maxWidth: 360, zIndex: 1000,
        background: "var(--card)", border: "1px solid rgba(var(--neg-rgb), .5)",
        borderRadius: "var(--radius)", padding: "14px 16px", boxShadow: "0 8px 24px rgba(0,0,0,.4)",
        display: "flex", gap: 12, alignItems: "flex-start",
      }}
    >
      <p style={{ margin: 0, fontSize: 13, color: "var(--text)", flex: 1, lineHeight: 1.6 }}>{message}</p>
      <button
        type="button"
        aria-label={dismissLabel}
        onClick={onDismiss}
        style={{
          background: "none", border: "none", color: "var(--text-dim)", cursor: "pointer",
          fontSize: 16, lineHeight: 1, padding: 0,
        }}
      >
        ×
      </button>
    </div>
  );
}
