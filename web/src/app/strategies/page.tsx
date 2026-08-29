"use client";
/**
 * `/strategies` — 策略列表頁（Task 9，設計稿 §04 的入口；卡片本體是 Task 8 的
 * `StrategyCard`，這裡不重複卡片內部規格）。
 *
 * ⭐ 與首頁「可跟單策略」區塊刻意共用文案（`COPY.home.strategies.*`）：兩處呈現
 * 同一批資料、同一套規則（`listable`＝`enabled` 且 `accepting_new`），拆兩份
 * 文案只會製造不同步的風險。
 */
import Link from "next/link";
import { useEffect, useState } from "react";
import { StrategyCard } from "@/components/StrategyCard";
import { useCopy } from "@/lib/lang";
import { getPublicStrategies, type PublicStrategy } from "@/lib/publicApi";

export default function StrategiesPage() {
  const COPY = useCopy();
  const home = COPY.home;
  const [strategies, setStrategies] = useState<PublicStrategy[]>([]);

  useEffect(() => {
    let cancelled = false;
    getPublicStrategies().then((r) => {
      if (!cancelled) setStrategies(r.strategies);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="page strategies-page">
      <header className="strategies-page-head">
        <h1>{home.strategies.heading}</h1>
        <p className="section-sub">{home.strategies.sub}</p>
      </header>
      <div className="strategy-grid">
        {strategies.map((s) => (
          <StrategyCard key={s.slug} strategy={s} />
        ))}
        {strategies.length === 0 && <p className="hint">{home.strategies.empty}</p>}
        <div className="card strategy-card strategy-card-advanced">
          <div className="strategy-card-name">{home.strategies.advancedTitle}</div>
          <p className="strategy-card-advanced-body">{home.strategies.advancedBody}</p>
          <Link href="/advanced" className="btn btn-secondary btn-block">
            {home.strategies.advancedCta}
          </Link>
        </div>
      </div>
    </main>
  );
}
