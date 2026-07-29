"use client";
import Link from "next/link";
import { COPY } from "@/lib/copy";
import { useMe, useOnboardingStatus } from "@/lib/hooks";

/**
 * 「這個帳號還沒開通完成」的提示，目前由 /leaders 使用。
 *
 * 為什麼需要：這兩頁在未活化狀態下**看起來完全正常**——清單載得出來、滑桿拖得動、
 * 按鈕按得下去。使用者會一路走到錢包簽署，才在最後一步撞上後端的「查無 follower」。
 * 把這件事講在前面，成本是一個面板，省下的是一次白跑的簽署流程。
 *
 * ⭐ 判定刻意 **fail-safe：只有在確知未活化時才提示**。`status.data` 缺席（載入中、
 * 網路錯、端點掛掉）一律不顯示——「你還沒開通」對一個其實已經開通的人來說是假警報，
 * 而假警報會讓人去點一個他根本不需要的流程。不確定時什麼都不說，比猜錯好。
 *
 * 這是提示，不是閘門：本元件不 disable 任何按鈕，也不擋任何流程。能不能真的送出由
 * 後端決定（沒有 follower 的請求後端本來就會擋），前端不在這裡複製一份授權判斷。
 */
export function ActivationNotice() {
  const me = useMe();
  const status = useOnboardingStatus({ enabled: !!me.data, pollMs: false });

  if (!status.data || status.data.state === "READY") return null;

  return (
    <div className="panel ops-notice">
      <p className="ops-notice-title">{COPY.common.notActivated}</p>
      <p>
        <Link className="btn btn-secondary" href="/onboarding">
          {COPY.common.goOnboarding}
        </Link>
      </p>
    </div>
  );
}
