"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useCopy } from "@/lib/lang";
import { getPublicStatus, type PublicComponentStatus } from "@/lib/publicApi";

/**
 * Footer — 四欄（品牌+免責 / 產品 / 可驗證 / 法務與聯絡）＋系統狀態燈（Task 7）。
 *
 * 狀態燈讀 `/api/public/status`（無需登入，見 lib/publicApi.ts）：**載入一次，
 * 不輪詢**（規格明講不需要 polling）。⭐ M3 round3 Task 9（R2 P2）：ok→綠「系統運作
 * 正常」、degraded→黃；`unknown`／讀取失敗／載入中三種情況**整顆燈連文字都不渲染**
 * （DOM 不存在，不是 visibility hidden）——舊版「狀態未知」灰字長期常駐等於向使用者
 * 宣告一個沒有資訊量的狀態，不如不顯示（讀不到 ≠ 系統健康，工程原則 3 的前端鏡射：
 * 讀不到就不宣告，不偽裝成「已知的未知」）。
 *
 * 法務欄連向 /terms /privacy /risk（Task 12 建立的實體頁）與 /contact（2026-09-02
 * 起改為站內聯絡表單頁，取代先前的 filet.app 外部連結）；產品／可驗證兩欄大多仍是純文字（對應頁面/錨點多數
 * 尚未存在，見設計稿 §03 L399-405 的靜態 mock，本任務不擅自發明路由）——
 * ⭐ Task 12 順帶把**已經有真實去處**的三項接上連結：文件 → `/docs`、
 * 系統狀態 → `/status`、績效方法論 → `/docs#methodology`（三者都是本 task
 * 建立的頁面／錨點）。其餘項目（策略、運作方式、費用、leader 帳戶、builder
 * fee 費率）仍無對應路由/錨點，維持純文字。
 */
export function Footer() {
  const COPY = useCopy();
  const c = COPY.footer;
  // `null` = 尚未載入完成（初始態）。`getPublicStatus` 本身已把讀取失敗／格式異常
  // 折疊成 `"unknown"`（見 lib/publicApi.ts），這裡再把 `"unknown"` 與 `null` 一起
  // 折成「不渲染」——三種情況（載入中／unknown／讀取失敗）在畫面上完全等價。
  const [status, setStatus] = useState<PublicComponentStatus | null>(null);

  useEffect(() => {
    let cancelled = false;
    getPublicStatus().then((s) => {
      if (!cancelled) setStatus(s.status);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const statusLabel = status === "ok" ? c.statusOk : status === "degraded" ? c.statusDegraded : null;

  return (
    <footer className="app-footer">
      <div className="footer-cols">
        <div className="footer-brand">
          <div className="wordmark-mini">{COPY.common.appName}</div>
          <p>{c.brandTagline}</p>
          {statusLabel != null && (
            <div className="footer-status" data-status={status}>
              <span className="status-dot" aria-hidden="true" />
              <span>{statusLabel}</span>
            </div>
          )}
        </div>
        <div>
          <div className="footer-col-title">{c.productTitle}</div>
          <div className="footer-col-list">
            <span>{c.productStrategies}</span>
            <span>{c.productHow}</span>
            <span>{c.productFees}</span>
          </div>
        </div>
        <div>
          <div className="footer-col-title">{c.verifiableTitle}</div>
          <div className="footer-col-list">
            <span>{c.verifiableLeaderAccounts}</span>
            <span>{c.verifiableBuilderFee}</span>
            <Link href="/docs#methodology">{c.verifiableMethodology}</Link>
            <Link href="/status">{c.verifiableStatus}</Link>
          </div>
        </div>
        <div>
          <div className="footer-col-title">{c.legalTitle}</div>
          <div className="footer-col-list">
            <Link href="/terms">{c.legalTerms}</Link>
            <Link href="/privacy">{c.legalPrivacy}</Link>
            <Link href="/risk">{c.legalRisk}</Link>
            <Link href="/contact">{c.legalContact}</Link>
          </div>
        </div>
      </div>
      <div className="footer-bottom">
        <p className="footer-disclaimer">{c.disclaimer}</p>
        <span className="mono">{c.copyright}</span>
      </div>
    </footer>
  );
}
