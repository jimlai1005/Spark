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
 */
export function FeeCalculator() {
  const COPY = useCopy();
  const c = COPY.fee;
  const [notional, setNotional] = useState(DEFAULT_NOTIONAL);
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
