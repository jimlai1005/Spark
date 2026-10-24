"use client";
import { useCopy } from "@/lib/lang";
import type { WalletConnectorLike } from "@/lib/walletChoice";

/**
 * WalletPicker — 多個瀏覽器擴充套件錢包並存時的選擇彈窗（2026-10-24，EIP-6963）。
 *
 * 純展示元件：候選清單、選了哪個、取消，全部交回呼叫端（`useWalletLogin`）。
 * 沿站內 `.modal-overlay` / `.modal-card` 樣式（同 ConfirmDialog）。
 * ⚠️ 呼叫端只在使用者按下 CTA 之後才渲染本元件——server 端的 connector 清單
 * 只有 injected 後備，client 才有發現到的錢包，先渲染會 hydration mismatch。
 * icon 是錢包自己透過 EIP-6963 宣告的 data URI，直接用 <img>（不是 next/image：
 * data URI 不走最佳化，且來源不是我們的網域）。
 */
export function WalletPicker<C extends WalletConnectorLike>({ candidates, busy, onPick, onCancel }: {
  candidates: readonly C[];
  busy?: boolean;
  onPick: (connector: C) => void;
  onCancel: () => void;
}) {
  const COPY = useCopy();
  const c = COPY.login;
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={c.pickerTitle}>
      <div className="modal-card card">
        <h3>{c.pickerTitle}</h3>
        <p className="hint">{c.pickerHint}</p>
        <ul className="wallet-picker-list">
          {candidates.map((connector) => (
            <li key={connector.id}>
              <button
                type="button"
                className="btn btn-secondary btn-block wallet-picker-item"
                disabled={busy}
                onClick={() => onPick(connector)}
              >
                {connector.icon && (
                  <>
                    {/* eslint-disable-next-line @next/next/no-img-element -- 錢包宣告的 data URI，不走 next/image */}
                    <img src={connector.icon} alt="" width={24} height={24} className="wallet-picker-icon" />
                  </>
                )}
                <span>{connector.name}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="step-actions">
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            {c.pickerCancel}
          </button>
        </div>
      </div>
    </div>
  );
}
