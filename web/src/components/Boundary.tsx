import type { ReactNode } from "react";

export interface BoundaryPanelItem {
  dt: string;
  dd: ReactNode;
  mono?: boolean;
}

export interface BoundaryProps {
  walletTitle: string;
  walletItems: BoundaryPanelItem[];
  engineTitle: string;
  engineItems: BoundaryPanelItem[];
  threadPct: number; // 0–100
  pillText: string;
  pillActive: boolean;
}

export function Boundary(p: BoundaryProps) {
  return (
    <div className="boundary" style={{ ["--thread-pct" as string]: p.threadPct }}>
      <div className="boundary-panel">
        <p className="eyebrow">{p.walletTitle}</p>
        <dl>
          {p.walletItems.map((it) => (
            <BoundaryRow key={it.dt} item={it} />
          ))}
        </dl>
      </div>
      <div className="boundary-link">
        <div className="boundary-divider" aria-hidden="true" />
        <div className="boundary-thread">
          <span className="boundary-thread-fill" />
        </div>
        <span className={`boundary-pill${p.pillActive ? " is-active" : ""}`}>{p.pillText}</span>
      </div>
      <div className="boundary-panel">
        <p className="eyebrow">{p.engineTitle}</p>
        <dl>
          {p.engineItems.map((it) => (
            <BoundaryRow key={it.dt} item={it} />
          ))}
        </dl>
      </div>
    </div>
  );
}

function BoundaryRow({ item }: { item: BoundaryPanelItem }) {
  return (
    <>
      <dt>{item.dt}</dt>
      <dd className={item.mono ? "mono" : undefined}>{item.dd}</dd>
    </>
  );
}
