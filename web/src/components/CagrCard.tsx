"use client";
/**
 * `CagrCard` — CAGR（年化）收合卡（M3 round3 Task 7 於 `strategies/[slug]`
 * 首建；M3 round4 Task R4-11 抽成共用元件，供 `/traders/[address]` 一併使用
 * ——後端 `build_cagr_fields` 對兩個端點供給同一套結構性防呆契約，見
 * `filet/strategies.py` 檔頭）。
 *
 * 呼叫端已用 `cagr_pct != null` 守門——本元件只在後端明確給出 CAGR 值時才被
 * 渲染（`sample_days<sample_threshold` 時整個不出現在 DOM，不再有「樣本不足」
 * 灰字佔位）。`cagr` 因此恆為非 null 字串。
 */
import { useState } from "react";
import type { COPY_ZH, DeepString } from "@/lib/copy";

export type CagrCopy = DeepString<typeof COPY_ZH.strategyDetail.cagr>;

export function CagrCard({ cagr, sampleDays, copy }: {
  cagr: string;
  sampleDays: number;
  copy: CagrCopy;
}) {
  const [open, setOpen] = useState(true);
  return (
    <div className="card cagr-card">
      <div className="cagr-card-value-col">
        <div className="metric-card-label">{copy.heading}</div>
        <div className="mono cagr-card-value">{cagr}%</div>
      </div>
      <div className="cagr-card-note-col">
        <button type="button" className="cagr-toggle" onClick={() => setOpen(!open)}>
          {open ? copy.toggleHide : copy.toggleShow}
        </button>
        {open && (
          <p className="cagr-note">
            {copy.notePrefix}
            {sampleDays}
            {copy.noteSuffix}
          </p>
        )}
      </div>
    </div>
  );
}
