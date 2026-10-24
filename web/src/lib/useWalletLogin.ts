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
 * 多錢包一律先問（2026-10-24 複審裁決）：wagmi 自動重連會讓回訪者 isConnected 為 true，
 * 若已連線就直接簽，回訪者永遠看不到選單；所以偵測到多個錢包時不論是否已連線都出選單，
 * `choose()` 選到目前已連的 connector 則跳過 connect 直接簽（否則 wagmi 丟
 * ConnectorAlreadyConnectedError），選別的才 connectAsync（wagmi 允許切換）。
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
  const { address, chainId, isConnected, connector: currentConnector } = useAccount();
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

  // 已連線時直接簽（不 connect）；錯誤分類與 phase 復位集中在這裡。
  const finishGuarded = useCallback(async (addr: string, cid: number) => {
    try {
      await finish(addr, cid);
    } catch (err) {
      fail(err);
    } finally {
      setPhase("idle");
    }
  }, [fail, finish]);

  const start = useCallback(async () => {
    setError(null);
    const choice = pickWalletCandidates(connectors);
    // 多錢包一律先問（即使 wagmi 已自動重連其中一個）：回訪者才有機會換錢包登入。
    if (choice.kind === "multiple") {
      setCandidates(choice.candidates);
      setPickerOpen(true);
      return;
    }
    if (isConnected && address && chainId) {
      await finishGuarded(address, chainId);
      return;
    }
    if (choice.kind === "none") {
      setError(COPY.login.noWallet);
      return;
    }
    await connectThen(choice.candidates[0]);
  }, [COPY, address, chainId, connectThen, connectors, finishGuarded, isConnected]);

  const choose = useCallback(async (connector: Connector) => {
    setPickerOpen(false);
    // 選到的就是目前已連的 connector → 不重連（wagmi 會丟 ConnectorAlreadyConnectedError），直接簽。
    if (isConnected && address && chainId && currentConnector && connector.id === currentConnector.id) {
      await finishGuarded(address, chainId);
      return;
    }
    await connectThen(connector);
  }, [address, chainId, connectThen, currentConnector, finishGuarded, isConnected]);

  const cancelPicker = useCallback(() => setPickerOpen(false), []);

  return { phase, error, pickerOpen, candidates, start, choose, cancelPicker };
}
