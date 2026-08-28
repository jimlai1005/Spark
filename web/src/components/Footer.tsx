"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useCopy } from "@/lib/lang";
import { getPublicStatus, type PublicComponentStatus } from "@/lib/publicApi";

/**
 * Footer — 四欄（品牌+免責 / 產品 / 可驗證 / 法務與聯絡）＋系統狀態燈（Task 7）。
 *
 * 狀態燈讀 `/api/public/status`（無需登入，見 lib/publicApi.ts）：**載入一次，
 * 不輪詢**（規格明講不需要 polling）。三態：ok→綠「系統運作正常」、
 * degraded→黃、unknown→灰「狀態未知」——載入中與任何讀取失敗都落在 unknown
 * （讀不到 ≠ 系統健康，工程原則 3 的前端鏡射）。
 *
 * 法務欄連向 /terms /privacy /risk（Task 12 建立的實體頁）與
 * mailto:contact@filet.trade；產品／可驗證兩欄大多仍是純文字（對應頁面/錨點多數
 * 尚未存在，見設計稿 §03 L399-405 的靜態 mock，本任務不擅自發明路由）——
 * ⭐ Task 12 順帶把**已經有真實去處**的三項接上連結：文件 → `/docs`、
 * 系統狀態 → `/status`、績效方法論 → `/docs#methodology`（三者都是本 task
 * 建立的頁面／錨點）。其餘項目（策略、運作方式、費用、leader 帳戶、builder
 * fee 費率）仍無對應路由/錨點，維持純文字。
 */
export function Footer() {
  const COPY = useCopy();
  const c = COPY.footer;
  const [status, setStatus] = useState<PublicComponentStatus>("unknown");

  useEffect(() => {
    let cancelled = false;
    getPublicStatus().then((s) => {
      if (!cancelled) setStatus(s.status);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const statusLabel =
    status === "ok" ? c.statusOk : status === "degraded" ? c.statusDegraded : c.statusUnknown;

  return (
    <footer className="app-footer">
      <div className="footer-cols">
        <div className="footer-brand">
          <div className="wordmark-mini">{COPY.common.appName}</div>
          <p>{c.brandTagline}</p>
          <div className="footer-status" data-status={status}>
            <span className="status-dot" aria-hidden="true" />
            <span>{statusLabel}</span>
          </div>
        </div>
        <div>
          <div className="footer-col-title">{c.productTitle}</div>
          <div className="footer-col-list">
            <span>{c.productStrategies}</span>
            <span>{c.productHow}</span>
            <span>{c.productFees}</span>
            <Link href="/docs">{c.productDocs}</Link>
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
            <a href="mailto:contact@filet.trade">{c.legalContact}</a>
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
