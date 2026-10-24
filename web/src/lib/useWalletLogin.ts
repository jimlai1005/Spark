"use client";
/**
 * lib/useWalletLogin.ts — 連接錢包 → SIWE 登入的單一入口（2026-10-24）。
 *
 * 取代 Header／advanced／strategies/[slug]／traders/[address]／ReconnectGate 五處
 * 各自複製的 `connectors[0]` → `connectAsync` → `loginWithSiwe` 流程，並加上
 * EIP-6963 多錢包選單：
 *   - 候選規則在 lib/walletChoice.ts（純函式）；
 *   - 選單 UI 由呼叫端用 `pickerOpen`／`candidates`／`choose`／`cancelPicker`
 *     渲染 <WalletPicker>（只在 pickerOpen 時渲染，避免 SSR hydration mismatch）；
 *   - 登入成功後 `await queryClient.resetQueries()`：身份切換整包清
 *     （2026-09-01 快取殘留事故，見 Header.handleLogout 註解），再呼叫 onSuccess。
 * `connectOnly`：只連錢包不簽（ReconnectGate：server session 仍有效、只是錢包斷了）。
 * 錯誤分類不得把 err 原文印出（可能含簽章／訊息原文）。
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { useAccount, useConnect, useSignMessage, type Connector } from "wagmi";
import type { Me } from "./api";
import { useCopy } from "./lang";
import { loginWithSiwe } from "./siwe";
import { classifyLoginError, pickWalletCandidates } from "./walletChoice";

export type LoginPhase = "idle" | "connecting" | "signing";

export interface UseWalletLoginOptions {
  /** 登入（或 connectOnly 時連線）成功後呼叫；login 模式帶 Me，connectOnly 帶 undefined。 */
  onSuccess?: (me: Me | undefined) => void | Promise<void>;
  /** 只連錢包、不做 SIWE 也不 resetQueries。 */
  connectOnly?: boolean;
}

export interface WalletLogin {
  phase: LoginPhase;
  error: string | null;
  pickerOpen: boolean;
  candidates: readonly Connector[];
  start: () => Promise<void>;
  choose: (connector: Connector) => Promise<void>;
  cancelPicker: () => void;
}

export function useWalletLogin({ onSuccess, connectOnly = false }: UseWalletLoginOptions = {}): WalletLogin {
  const COPY = useCopy();
  const queryClient = useQueryClient();
  const { address, chainId, isConnected } = useAccount();
  const { connectAsync, connectors } = useConnect();
  const { signMessageAsync } = useSignMessage();
  const [phase, setPhase] = useState<LoginPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [candidates, setCandidates] = useState<readonly Connector[]>([]);

  const fail = useCallback((err: unknown) => {
    const kind = classifyLoginError(err);
    setError(kind === "rejected" ? COPY.login.rejected : kind === "noWallet" ? COPY.login.noWallet : COPY.login.loginFailed);
  }, [COPY]);

  const finish = useCallback(async (addr: string, cid: number) => {
    if (connectOnly) {
      await onSuccess?.(undefined);
      return;
    }
    setPhase("signing");
    const me = await loginWithSiwe({
      address: addr,
      chainId: cid,
      signMessage: (message) => signMessageAsync({ message }),
    });
    await queryClient.resetQueries();
    await onSuccess?.(me);
  }, [connectOnly, onSuccess, queryClient, signMessageAsync]);

  const connectThen = useCallback(async (connector: Connector) => {
    try {
      setPhase("connecting");
      const result = await connectAsync({ connector });
      const addr = result.accounts[0];
      if (!addr) {
        setError(COPY.login.noWallet);
        return;
      }
      await finish(addr, result.chainId);
    } catch (err) {
      fail(err);
    } finally {
      setPhase("idle");
    }
  }, [COPY, connectAsync, fail, finish]);

  const start = useCallback(async () => {
    setError(null);
    if (isConnected && address && chainId) {
      try {
        await finish(address, chainId);
      } catch (err) {
        fail(err);
      } finally {
        setPhase("idle");
      }
      return;
    }
    const choice = pickWalletCandidates(connectors);
    if (choice.kind === "none") {
      setError(COPY.login.noWallet);
      return;
    }
    if (choice.kind === "single") {
      await connectThen(choice.candidates[0]);
      return;
    }
    setCandidates(choice.candidates);
    setPickerOpen(true);
  }, [COPY, address, chainId, connectThen, connectors, fail, finish, isConnected]);

  const choose = useCallback(async (connector: Connector) => {
    setPickerOpen(false);
    await connectThen(connector);
  }, [connectThen]);

  const cancelPicker = useCallback(() => setPickerOpen(false), []);

  return { phase, error, pickerOpen, candidates, start, choose, cancelPicker };
}
