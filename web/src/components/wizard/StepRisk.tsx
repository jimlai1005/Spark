"use client";
import { useState } from "react";
import { useCopy } from "@/lib/lang";

export function StepRisk({ onConfirm }: { onConfirm: () => void }) {
  const [agree, setAgree] = useState([false, false, false, false]);
  const COPY = useCopy();
  const c = COPY.wizard;
  const rows = [c.risk1, c.risk2, c.risk3, c.risk4];
  return (
    <div className="step-card">
      <p className="eyebrow">02・{c.stepNames[1]}</p>
      <h2>{c.step2Title}</h2>
      {rows.map((text, i) => (
        <label key={text} className="check-row">
          <input
            type="checkbox"
            checked={agree[i]}
            onChange={(e) => {
              const next = [...agree];
              next[i] = e.target.checked;
              setAgree(next);
            }}
          />
          <span>{text}</span>
        </label>
      ))}
      <p className="hint">{c.fundsWarning}</p>
      <div className="step-actions">
        <button type="button" className="btn btn-primary"
          disabled={!agree.every(Boolean)} onClick={onConfirm}>
          {COPY.common.next}
        </button>
      </div>
      <p className="hint">{COPY.common.nonCustodial}</p>
    </div>
  );
}
