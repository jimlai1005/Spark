"use client";
import { useState } from "react";
import { fmtAmount } from "@/lib/format";
import { useCopy } from "@/lib/lang";

// $1,000–$100,000，step $1,000，預設 $10,000（plan Task 8 錨例）。
const MIN_NOTIONAL = 1000;
const MAX_NOTIONAL = 100000;
const STEP_NOTIONAL = 1000;
const DEFAULT_NOTIONAL = 10000;

// 建倉／平倉各收一次 builder fee 上限（0.02% = 2bp），純 client 端試算，
// 不代表任何已送出的交易——真正收費以鏈上 builder code 記錄為準。
const FEE_RATE = 0.0002;

/**
 * FeeCalculator — 費用試算 slider（Task 8，NOTE 06）。
 * 錨例：notional=10000 → side=2.00、total=4.00；notional=100000 → total=40.00。
 *
 * ⭐ `initialNotional`（Task 10 新增，onboarding step 4 用來預填投入額）：純初始值，
 * 不是受控值——slider 一旦掛載即由使用者自行操作，呼叫端變更這個 prop 不會回頭
 * 覆蓋使用者已經調整過的值（沿 React 的 uncontrolled-initial-state 慣例，避免
 * 使用者調整途中被外部 re-render 打斷）。缺省沿用既有 `DEFAULT_NOTIONAL`，
 * 首頁既有呼叫點行為不變。夾在 [MIN_NOTIONAL, MAX_NOTIONAL] 之間，避免呼叫端傳入
 * 超出 slider 範圍的值。
 */
export function FeeCalculator({ initialNotional }: { initialNotional?: number } = {}) {
  const COPY = useCopy();
  const c = COPY.fee;
  const clampedInitial = initialNotional == null
    ? DEFAULT_NOTIONAL
    : Math.min(MAX_NOTIONAL, Math.max(MIN_NOTIONAL, Math.round(initialNotional / STEP_NOTIONAL) * STEP_NOTIONAL));
  const [notional, setNotional] = useState(clampedInitial);
  const side = notional * FEE_RATE;
  const total = side * 2;

  return (
    <section className="fee-section">
      <div className="fee-copy">
        <h2>{c.heading}</h2>
        <p>{c.body}</p>
        <p className="fee-note">{c.note}</p>
      </div>
      <div className="card fee-calc">
        <div className="fee-calc-label">{c.calcLabel}</div>
        <div className="fee-calc-notional mono">USD {fmtAmount(String(notional), 0)}</div>
        <input
          type="range"
          className="fee-calc-slider"
          min={MIN_NOTIONAL}
          max={MAX_NOTIONAL}
          step={STEP_NOTIONAL}
          value={notional}
          aria-label={c.calcLabel}
          onChange={(e) => setNotional(Number(e.target.value))}
        />
        <div className="fee-calc-rows mono">
          <div className="fee-calc-row">
            <span>{c.openLabel}</span>
            <span>USD {fmtAmount(String(side), 2)}</span>
          </div>
          <div className="fee-calc-row">
            <span>{c.closeLabel}</span>
            <span>USD {fmtAmount(String(side), 2)}</span>
          </div>
          <div className="fee-calc-row fee-calc-total">
            <span>{c.totalLabel}</span>
            <span>USD {fmtAmount(String(total), 2)}</span>
          </div>
        </div>
      </div>
    </section>
  );
}
