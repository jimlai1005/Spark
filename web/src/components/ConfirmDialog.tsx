"use client";

/**
 * 輕量確認彈窗（M3 round4 Task R4-4，使用者裁決 4）——沿站內既有 modal 樣式
 * （`.modal-overlay` / `.modal-card`，同 `CloseAllModal` 的外層結構），但不帶
 * 二次勾選確認框：用於低危害、可逆的動作（暫停／恢復跟單）。平倉這種不可逆
 * 動作仍走專屬的 `CloseAllModal`（勾選確認＋簽章），不共用本元件。
 *
 * 純展示元件：呼叫端自行管理 open/busy 狀態與實際的 API 呼叫；取消一律只
 * 呼叫 `onCancel`，不觸發任何副作用。
 */
export function ConfirmDialog({
  title, body, confirmLabel, cancelLabel, busy, onConfirm, onCancel,
}: {
  title: string;
  body: string;
  confirmLabel: string;
  cancelLabel: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="modal-overlay" role="dialog" aria-modal="true" aria-label={title}>
      <div className="modal-card card">
        <h3>{title}</h3>
        <p className="hint">{body}</p>
        <div className="step-actions">
          <button type="button" className="btn btn-secondary" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button type="button" className="btn btn-primary" onClick={onConfirm} disabled={busy}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
