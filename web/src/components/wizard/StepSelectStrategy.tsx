"use client";
import Link from "next/link";
import { StrategyCard } from "@/components/StrategyCard";
import { shortAddr } from "@/lib/format";
import { useCopy } from "@/lib/lang";
import type { PublicStrategyDetail } from "@/lib/publicApi";

/**
 * StepSelectStrategy — onboarding step 1（設計稿 §05：「進頁即完成態」，不是一個
 * 使用者要走過的步驟，是一個一直顯示在其他步驟之上的摘要卡＋「返回改選」）。
 *
 * 兩種選定形式（Task 11 的 `/advanced` 會產生 `advanced:0x…` 形式）：
 * - 精選策略：`detail` 為 `PublicStrategyDetail`，直接復用 `StrategyCard`（Task 8）。
 * - 進階模式：只有一個位址，未經任何盡職審查／背書——明確標示，不得讓它看起來
 *   像一張精選策略卡（紅線：不得暗示背書）。
 */
export function StepSelectStrategy({ isAdvanced, advancedAddress, detail }: {
  isAdvanced: boolean;
  advancedAddress: string | null;
  /** `undefined`＝載入中；`null`＝查無此策略（404 或讀取失敗）。僅非進階模式適用。 */
  detail: PublicStrategyDetail | null | undefined;
}) {
  const COPY = useCopy();
  const c = COPY.wizard;

  return (
    <div className="onboard-strategy-summary">
      <p className="eyebrow">{c.step1Eyebrow}</p>
      {isAdvanced && advancedAddress ? (
        <div className="card strategy-card strategy-card-advanced">
          <div className="strategy-card-name mono">{shortAddr(advancedAddress)}</div>
          <span className="pill chip-pending">{c.step1AdvancedLabel}</span>
          <p className="strategy-card-advanced-body">{c.step1AdvancedBody}</p>
        </div>
      ) : detail === undefined ? (
        <p className="hint">{COPY.common.loading}</p>
      ) : detail === null ? (
        <p className="hint">{c.step1NotFound}</p>
      ) : (
        <StrategyCard strategy={detail} />
      )}
      <Link className="btn btn-ghost" href="/strategies">{c.step1Back}</Link>
    </div>
  );
}
