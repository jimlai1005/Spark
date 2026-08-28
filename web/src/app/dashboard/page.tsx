"use client";
/**
 * `/dashboard` — 使用者 Dashboard 六塊＋持倉（Task 14，設計稿 §06＋NOTE 13-18）。
 *
 * 唯一資料源是 `GET /api/me/dashboard`（Task 13）：一次回六個獨立 nullable 的塊＋
 * 持倉列表。任何一塊為 `null` 都不是白畫面——各卡片自己處理 `null`，渲染保守的
 * 「—」（`format.NO_VALUE`），不臆造數字（不變量 6）。
 *
 * 未登入 → redirect `/strategies`（與 onboarding/settings 等既有頁同一慣例，
 * guard 用 effect 避免在 render 期間呼叫 router.push）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { EquityCard } from "@/components/dashboard/EquityCard";
import { ExposureCard } from "@/components/dashboard/ExposureCard";
import { FeesCard } from "@/components/dashboard/FeesCard";
import { PnlCard } from "@/components/dashboard/PnlCard";
import { PositionsTable } from "@/components/dashboard/PositionsTable";
import { StatusCard } from "@/components/dashboard/StatusCard";
import { SyncCard } from "@/components/dashboard/SyncCard";
import { ApiError, getDashboard, type DashboardResp } from "@/lib/api";
import { shortAddr } from "@/lib/format";
import { useMe } from "@/lib/hooks";
import { useCopy } from "@/lib/lang";

/** `updated_at`（epoch 秒）→「Xs 前」/「Xm 前」/「Xh 前」，供頂欄「最後同步」用。 */
function relativeAgo(updatedAt: number, nowS: number): string {
  const diff = Math.max(0, Math.floor(nowS - updatedAt));
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  return `${Math.floor(diff / 3600)}h`;
}

export default function DashboardPage() {
  const router = useRouter();
  const me = useMe();
  const loggedIn = !!me.data;
  const COPY = useCopy();
  const c = COPY.dashboard;
  const queryClient = useQueryClient();

  // ⭐ Task 15：平倉並撤銷送出後開始輪詢，直到 status.state 變成 "halted"
  // 才停下——收尾是引擎下一輪（約一分鐘）才會完成的非同步動作，客戶要能看到
  // 進度而不是送出後畫面一片空白。其餘時間維持一次性讀取（不背景輪詢），
  // 不對未觸發 kill switch 的使用者加無謂的背景請求。
  const [awaitingHalt, setAwaitingHalt] = useState(false);

  const dash = useQuery<DashboardResp>({
    queryKey: ["me-dashboard"],
    queryFn: async () => {
      try {
        return await getDashboard();
      } catch (e) {
        if (e instanceof ApiError && e.kind === "auth") {
          queryClient.invalidateQueries({ queryKey: ["me"] });
        }
        throw e;
      }
    },
    enabled: loggedIn,
    refetchInterval: awaitingHalt ? 5000 : false,
  });

  useEffect(() => {
    if (dash.data?.status?.state === "halted") setAwaitingHalt(false);
  }, [dash.data?.status?.state]);

  // 未登入一律 redirect /strategies（不在 render 期間呼叫 router.push，guard 用 effect，
  // 與 onboarding/page.tsx NOTE 10 同慣例）。
  useEffect(() => {
    if (me.isLoading) return;
    if (!me.data) router.push("/strategies");
  }, [me.isLoading, me.data, router]);

  if (me.isLoading || !me.data || dash.isLoading) {
    return (
      <main className="page">
        <p className="hint">{c.loadingNote}</p>
      </main>
    );
  }

  const data = dash.data ?? null;
  const nowS = Date.now() / 1000;

  return (
    <main className="page dash-page">
      <div className="dash-headrow">
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <h1>{c.heading}</h1>
          <span className="mono dash-addr">{shortAddr(me.data.address)}</span>
        </div>
        {data && (
          <div className="dash-sync-meta">
            <span>
              {c.lastSyncPrefix}{relativeAgo(data.updated_at, nowS)}{c.lastSyncSuffix}
            </span>
            <span className="dash-live-pill">
              <span className="dash-live-dot" aria-hidden="true" />
              {c.liveBadge}
            </span>
          </div>
        )}
      </div>

      <div className="dash-row1">
        <StatusCard
          status={data?.status ?? null}
          me={me.data}
          positions={data?.positions ?? null}
          closeAllPending={awaitingHalt}
          onActionSettled={() => void dash.refetch()}
          onCloseAllSubmitted={() => {
            setAwaitingHalt(true);
            void dash.refetch();
          }}
        />
        <EquityCard equity={data?.equity ?? null} />
      </div>

      <div className="dash-row2">
        <ExposureCard exposure={data?.exposure ?? null} />
        <PnlCard pnl={data?.pnl ?? null} />
      </div>

      <div className="dash-row3">
        <SyncCard sync={data?.sync ?? null} />
        <FeesCard feesMonth={data?.fees_month ?? null} />
      </div>

      <PositionsTable positions={data?.positions ?? null} feesMonth={data?.fees_month ?? null} />
    </main>
  );
}
