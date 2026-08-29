"use client";
/**
 * `/status` — 系統狀態頁（Task 12）。讀 `/api/public/status`（無需登入），渲染整體
 * 狀態＋components 清單＋updated_at。載入失敗一律降級為 unknown 態（`getPublicStatus`
 * 本身已經把連線失敗／非 200／格式異常折疊成 `UNKNOWN_STATUS`，見 lib/publicApi.ts
 * 檔頭——本頁不需要另外 catch，只要正確渲染它回傳的三態就滿足「不炸」的要求）。
 * 未登入可直接開啟，不掛登入 guard（同 /docs /terms /privacy /risk）。
 */
import { useEffect, useState } from "react";
import { useCopy } from "@/lib/lang";
import { getPublicStatus, type PublicStatus } from "@/lib/publicApi";
import { NO_VALUE } from "@/lib/format";

const INITIAL: PublicStatus = { status: "unknown", components: [], updated_at: 0 };

function formatUpdatedAt(epochSeconds: number): string {
  if (!epochSeconds) return NO_VALUE;
  const d = new Date(epochSeconds * 1000);
  if (Number.isNaN(d.getTime())) return NO_VALUE;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export default function StatusPage() {
  const COPY = useCopy();
  const c = COPY.status;
  const footer = COPY.footer;
  const [status, setStatus] = useState<PublicStatus>(INITIAL);

  useEffect(() => {
    let cancelled = false;
    getPublicStatus().then((s) => {
      if (!cancelled) setStatus(s);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const statusLabel =
    status.status === "ok" ? footer.statusOk
      : status.status === "degraded" ? footer.statusDegraded
        : footer.statusUnknown;

  return (
    <main className="page status-page">
      <header className="status-page-head">
        <h1>{c.heading}</h1>
        <p className="section-sub">{c.sub}</p>
      </header>

      <section className="card status-overall" data-status={status.status}>
        <span className="status-dot status-dot-lg" aria-hidden="true" />
        <span>{statusLabel}</span>
      </section>

      {status.status === "unknown" && status.components.length === 0 && (
        <p className="hint">{c.loadFailedNote}</p>
      )}

      <section className="status-components">
        <h2>{c.componentsHeading}</h2>
        {status.components.length === 0 ? (
          <p className="hint">{c.empty}</p>
        ) : (
          <ul>
            {status.components.map((comp) => {
              const label =
                comp.status === "ok" ? footer.statusOk
                  : comp.status === "degraded" ? footer.statusDegraded
                    : footer.statusUnknown;
              return (
                <li key={comp.name} className="card status-component-row" data-status={comp.status}>
                  <span className="status-component-left">
                    <span className="status-dot" aria-hidden="true" />
                    <span className="mono">{comp.name}</span>
                  </span>
                  <span className="pill status-component-pill" data-status={comp.status}>{label}</span>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <p className="mono status-updated-at">
        {COPY.strategyDetail.asOfPrefix}
        {formatUpdatedAt(status.updated_at)}
      </p>
    </main>
  );
}
