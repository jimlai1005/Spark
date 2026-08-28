"use client";
/**
 * lib/lang.tsx — 語言 context：LangProvider + useLang + useCopy。
 *
 * SSR 安全：首繪一律 "zh"（與伺服器渲染結果一致），`localStorage.filet_lang` 的讀取
 * 包在 `useEffect` 裡，在 effect 內才切換到記憶的語言——避免 hydration mismatch。
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { COPY_EN, COPY_ZH, type DeepString } from "./copy";

export type Lang = "zh" | "en";
type CopyDict = DeepString<typeof COPY_ZH>;

const STORAGE_KEY = "filet_lang";

const DICTS: Record<Lang, CopyDict> = {
  zh: COPY_ZH,
  en: COPY_EN,
};

interface LangContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
}

const LangContext = createContext<LangContextValue | undefined>(undefined);

function isLang(value: unknown): value is Lang {
  return value === "zh" || value === "en";
}

export function LangProvider({ children }: { children: ReactNode }) {
  // 首繪一律 "zh"：與 SSR 輸出一致，避免 hydration mismatch。
  const [lang, setLangState] = useState<Lang>("zh");

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      if (isLang(stored)) setLangState(stored);
    } catch {
      // localStorage 不可用（隱私模式等）：維持預設 "zh"。
    }
  }, []);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // 寫入失敗不影響當前 session 的語言狀態。
    }
  }, []);

  const value = useMemo(() => ({ lang, setLang }), [lang, setLang]);

  return <LangContext.Provider value={value}>{children}</LangContext.Provider>;
}

export function useLang(): LangContextValue {
  const ctx = useContext(LangContext);
  if (!ctx) throw new Error("useLang must be used within a LangProvider");
  return ctx;
}

export function useCopy(): CopyDict {
  const { lang } = useLang();
  return DICTS[lang];
}
