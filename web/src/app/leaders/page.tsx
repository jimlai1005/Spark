"use client";
/**
 * `/leaders` — 舊路由。Task 11 起功能全數遷移至 `/advanced`（見該檔檔頭）；
 * 本頁只負責 redirect，保留路由不 404（外部書籤／既有連結仍可能指向這裡）。
 *
 * `router.replace`（非 `push`，R-C／S1 審查修正）：純轉發頁不進瀏覽紀錄，
 * 避免使用者在 `/advanced` 按上一頁回到這裡又立刻被轉走。
 */
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useCopy } from "@/lib/lang";

export default function LeadersPage() {
  const router = useRouter();
  const COPY = useCopy();

  useEffect(() => {
    router.replace("/advanced");
  }, [router]);

  return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
}
