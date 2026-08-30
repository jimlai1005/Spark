"use client";
/**
 * `/leaderboard` — 舊路由。M3 round3 Task 4 起功能全數遷移至 `/explore`
 * （見該檔檔頭：重構為「可跟單對象探索」，見 plan `docs/superpowers/plans/
 * 2026-08-30-m3-ui-round3.md` D4）；本頁只負責 redirect，保留路由不 404
 * （外部書籤／既有連結仍可能指向這裡），沿用 `/leaders` → `/advanced` 的既有
 * redirect 慣例（見 `app/leaders/page.tsx`）。
 */
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useCopy } from "@/lib/lang";

export default function LeaderboardPage() {
  const router = useRouter();
  const COPY = useCopy();

  useEffect(() => {
    router.push("/explore");
  }, [router]);

  return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
}
