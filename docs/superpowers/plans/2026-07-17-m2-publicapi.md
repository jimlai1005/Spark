# M2 Public API（非託管 onboarding 後端）實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 非託管 copytrading 的 onboarding 後端：使用者用瀏覽器錢包 SIWE 登入 → 後端經 key-service 生成 agent key（只拿到地址）→ 後端建 ApproveAgent / ApproveBuilderFee 的 EIP-712 typed data（不簽）→ 前端錢包簽後**直接 POST HL `/exchange`**（後端不經手已簽授權；CORS 已實測全開，見設計定案 1）→ 後端以鏈上查詢確認（status/verify）→ verify 全過寫 pending → 人工 activate CLI 開引擎。**主鑰全程只在使用者瀏覽器，永不進後端任何欄位或 log；EIP-712 授權簽名也永不進後端。**

**Architecture:** FastAPI app（filet-api user、只綁 127.0.0.1、反代 TLS 在部署計畫）。所有外部依賴注入：`ApiStore`（SQLite：nonce 單次使用、session、onboarding 進度）、`KeysvcClient`（unix socket，元件 key-service 已完成）、`HLGateway`（對 HL 的唯一出口，**唯讀**：全部冪等查詢、transient 重試）。account_id 一律由 session 地址確定性衍生（`"f"+40hex`），端點無 account 參數——「別人不能替你 onboard」是結構保證不是檢查。

**Tech Stack:** Python 3.11 + uv、FastAPI + uvicorn、httpx（HL 讀取與 TestClient）、eth_account（SIWE 驗簽與測試簽名，真密碼學、本地運算不觸網）、hyperliquid-python-sdk 0.24.0（`user_signed_payload`；`recover_user_from_user_signed_action` 僅測試用作 SDK pin）、sqlite3（stdlib）、pytest（全離線）。

**Spec:** `docs/superpowers/specs/2026-07-17-m2-onboarding-dashboard-design.md`（不變量、三層隔離、account_id 規則、nonce 單次使用、**前端直送 HL**、activate 人工 CLI 以此為準）。
**Research:** `docs/superpowers/research/2026-07-17-hl-sdk-external-signing.md`（GO；動態 chainId、agentName 一律給落在 Task 5/9；v 正規化與 recover 預驗隨提交路徑移至**前端計畫**；「簽後立即提交」由前端執行）。
本計畫是 spec 實作拆解的**第 2 項（Public API）**；前端（第 3）與部署（第 4）各自後續計畫。

---

## 執行狀態（2026-07-17 完成）

**全 14 task + 2 追加修正實作 + fresh-context 雙審完成；`uv run pytest -q` = 662 passed, 2 deselected；ruff clean。** 分支 `feat/m2-publicapi`（自 `feat/m2-keyservice` 分出），未 push、未動 main。opus 兩輪把關：計畫級對抗審（REVISE_THEN_GO→修訂）＋ 完工後整體非託管總審（A-F 不變量成立，2 必修已修）。

| Task | commit | 備註 |
|---|---|---|
| 0 依賴+基線 | `39341cc` | uv.lock gitignored（repo 慣例）|
| 1 keysvc address op ⭐ | `69bf0ff` | spec deviation：唯讀擴充（desync 自癒），待使用者追認 |
| 2 config/identity | `4a59ff8` | 常數單一來源（`is` 斷言）|
| 3 SIWE ⭐ | `c18a140` | EIP-4361 對官方 ABNF 逐項驗過 |
| 4 ApiStore ⭐ | `dc18173` | nonce 原子單次使用 |
| 5 typed-data builder ⭐ | `6cadcfc` | 欄位逐字對 SDK；round-trip pin |
| 6 HLGateway | `4215193` | opus I1 落地：httpx→內建型別轉譯 |
| 7 app+auth ⭐ | `701f63f` | VerifyBody 無 address 欄（結構性）|
| 8 agent+status | `f642bff` | opus I2 落地：desync 自癒 |
| 9 payload ⭐ | `a8f7d1f` | 前端直送；submit 端點 404 測試 |
| 10 verify+pending+admin | `7961a61` | pending 原子冪等；admin 白名單 |
| 11 activate CLI ⭐ | `0524d5e` | builder pin 核對；fail-fast 重讀 |
| 12 入口+systemd | `5b3b923` | 127.0.0.1 only |
| 追加：502 統一轉譯 | `98fa258` | 兩位 reviewer 獨立點名的系統性缺口 |
| 13 端到端 ⭐ | `147e5b7` | 真 SIWE+真 keysvc+r/s/v 表面掃描+desync 契約幕 |
| 追加：keysvc client 邊界 | `6a770fe` | opus 總審 2 必修：截斷回應→transient；settimeout 10s |

**opus 總審結論**：非託管保證 A-F（主鑰不進後端／agent 私鑰只在 keysvc／後端不經手授權簽名／session 結構性隔離／nonce 原子／manifest 只由人工 CLI 寫）**結構上成立且有測試背書**。

**觀察項（非阻擋，記錄）**：pending.json 外部竄改損毀時 load 直接炸＝大聲安全失敗（可接受）；nonce/session 無 reaper（磁碟成長維運項）；CORS 依賴部署反代同源（移交部署計畫）；r/s/v 型別掃描偏窄但由 gateway 無 /exchange 出口 backstop；gateway 寫入面黑名單掃描＋/info 白名單雙測互補；activate 重讀驗證不回滾（docstring 已修辭精準）。

**移交前端計畫**：v 正規化（0/1→27/28）、簽後立即直送 HL /exchange、payload 組裝、（可選）前端側 recover 預驗。**移交部署計畫**：反代 TLS+同源、filet-api 讀不到 keys 的實機驗收、`REPLACE_WITH_FILET_API_UID` 填值。

---

## 全域紅線（每個任務的實作者與 reviewer 都先讀）

1. ⭐ **主鑰/助記詞永不進後端**：任何端點、任何欄位、任何 log 都不收不存主鑰或助記詞。後端只經手：SIWE 簽名、EIP-712 typed data（無私鑰即可建）、r/s/v 簽名值。
2. ⭐ **agent 私鑰只在 key-service**：API 只經 `KeysvcClient.generate` 拿到 agent **地址**；私鑰不進 API 進程、DB、回應、log。
3. ⭐ **所有 onboarding 端點綁 session 地址**：account_id 由 session 地址衍生（`derive_account_id`），端點無 account/address 輸入參數——別人不能替你 onboard 是結構保證。
4. ⭐ **SIWE nonce 單次使用**：consume 是原子 UPDATE（rowcount 判定），不是先查再改的 TOCTOU；用過/過期一律 401。SIWE 訊息由伺服器權威重建（domain/URI 出自設定），不解析前端自由文本。
5. ⭐ **後端不經手 EIP-712 授權簽名**：API 表面（所有 request/response model 與 log）不存在任何收 r/s/v 的欄位——結構性測試守（Task 13）。前端簽完直送 HL `/exchange`；後端唯一經手的簽名是 SIWE 登入簽名（EIP-191，身分驗證用，性質不同）。DB 只存地址與進度（無 action、無簽名、無私鑰）。
6. ⭐ **builder_address 是伺服器常數**：pending 條目的 builder_address 出自 `ApiConfig`、user_address 出自 session（spec opus 審查 m3）；activate CLI 再以 `FILET_BUILDER_ADDR` 結構性核對一次。
7. 測試全離線：autouse socket-ban（tests/conftest.py）不修改；TestClient 需要的本機 socketpair 沿 `tests/test_keysvc_client.py` 的 import 期捕捉＋monkeypatch 慣例。HL 與 keysvc 在單元測試一律注入 fake。
8. 不 push、不動 main；`~/projects/hl-copytrader` 唯讀不碰；不修改 engine/spec/research（`src/spark/keystore/`、`src/spark/filet/` 只 import 不改）。keysvc 僅允許 Task 1 的唯讀 `address` op＋結構化 error code 擴充（spec deviation，設計定案 12）——不動 generate 語意與金鑰生成/寫入路徑。

## 設計定案（spec 未定或與指揮官指示有出入處，本計畫拍板；審查時重點盯）

1. **提交路徑：採 spec 前端直送 HL `/exchange`**（2026-07-17 指揮官裁決，推翻先前的後端 submit 方案）。證據：HL API 對瀏覽器 CORS 全開——指揮官實測 mainnet 與 testnet `OPTIONS /exchange` 皆回 `access-control-allow-origin: *`、`access-control-allow-methods: *`、`access-control-allow-headers: *`（2026-07-17）。前端直送讓已簽授權（nonce 窗口內等同 bearer token）連後端都不經過，攻擊面更小。research §6 的 `submit_signed_action` 建議**不採**；其 recover 預驗 / v 正規化職責移至前端（已記入前端計畫待辦，見「不在本計畫」節）。提交結果由後端 `GET /api/onboard/status` 鏈上查詢確認。
2. **端點路徑**：spec 的 `/onboard/{account}/...` 改為 `/api/onboard/...`，account 從 session 衍生、無路徑參數（更小攻擊面；spec 的「驗 session == account」升級為「結構上無此輸入」）。
3. **SIWE**：手工組 EIP-4361 訊息 + eth_account 驗簽，不加 siwe 依賴。nonce 端點 `GET /api/auth/nonce?address=&chain_id=`（回 nonce＋完整 message，前端照簽），nonce 綁地址＋過期（spec 資料模型）。
4. **狀態落地**：SQLite（`FILET_API_DB`：nonces/sessions/onboarding）＋獨立 `pending.json`（filet-api 擁有）。**不改 `followers.py`/`FollowerRef`**；`followers.json` 只由人工 activate CLI 寫——權限拓撲上 web 層本就不該能寫引擎 manifest。
5. **activate CLI 檔名**：`scripts/filet_activate.py`（依 spec；指揮官 prompt 的 `activate_follower.py` 不採）。
6. **提交結果確認**：前端直送後，後端以 `GET /api/onboard/status` 鏈上查詢確認（狀態靠查詢、冪等、斷點續走，沿 M1 精神）——後端無提交路徑，也就沒有提交失敗分類問題；HL 端的提交失敗由前端處理（見「不在本計畫」）。
7. **status vs verify**：`GET /api/onboard/status` 純讀（斷點續走判準）；副作用（寫 pending）只在 `POST /api/onboard/verify`（spec 端點）。
8. **HL 讀取不經引擎的 `HyperliquidAdapter`**（它沒有 extraAgents 讀取，加了就動引擎碼）：publicapi 自帶 `HLGateway` 作單一 resilience boundary（工程原則 5），httpx 直呼 `/info`、`/exchange`。
9. **agentName 固定 `"filet"`**（research：一律給名字，避開 SDK「空名刪欄位」特例）。agent key 每次 onboarding 全新生成（key-service O_EXCL + 端點 409 防重生），絕不重用 agent 地址。
10. **`GET /perf/{account}` 不在本計畫**（dashboard 讀取面，指揮官範圍排除；見文末 spec 覆蓋對照）。
11. **地址比較基準**：一律 `normalize_address`（0x+40hex 小寫）後比對——SIWE recover vs nonce 綁定地址、agent 地址 vs extraAgents、admin 白名單、builder 核對，全部同基準（工程原則 1）。
12. **spec deviation——keysvc 加唯讀 `address` op**（2026-07-17 opus 審 I2 裁決）：spec 寫 key-service「無其他操作（不提供讀金鑰、不提供簽名）」，其意圖是「不讀鑰、不簽名、最小攻擊面」；`address` op 只回 agent **地址**（公開資訊，本來就在 generate 回應與鏈上 extraAgents），不違反意圖，且把「API DB 遺失 → generate 拒重生、地址拿不回 → 使用者永久卡死」變成自動復原（Task 1 實作、Task 8 自癒、Task 13 契約測試）。同步把 keysvc `Response` 加結構化 `code` 欄位，消滅跨進程中文訊息子字串比對（M3）。私鑰不出進程的紅線不變。

## 檔案結構（本計畫鎖定）

```
src/spark/keysvc/                # Task 1（Modify，spec deviation 見設計定案 12）
├── protocol.py                  #   加 AddressRequest(op="address") + Response.code
├── server.py                    #   generate 補 code、新增 handle_address（唯讀）、迴圈分派
└── client.py                    #   加 address()；KeysvcError(RuntimeError) 帶 .code
src/spark/publicapi/
├── __init__.py                  # Task 2（docstring）
├── config.py                    # Task 2：normalize_address / derive_account_id / ApiConfig
├── siwe.py                      # Task 3：EIP-4361 訊息重建 + recover
├── store.py                     # Task 4：SQLite（nonce 單次使用、session、onboarding 進度）
├── approvals.py                 # Task 5：ApproveAgent/ApproveBuilderFee typed-data builder
├── hl.py                        # Task 6：HLGateway（唯一 HL 出口，唯讀、冪等重試）
├── pending.py                   # Task 10：pending.json 讀寫（原子、冪等）
└── app.py                       # Task 7/8/9/10：FastAPI app factory 與全部端點
scripts/run_api.py               # Task 12：uvicorn 入口
scripts/filet_activate.py        # Task 11：人工核可 CLI
deploy/filet-api.service         # Task 12：systemd unit（filet-api user）
tests/
├── test_keysvc_protocol.py      # Task 1（Modify：address op + code）
├── test_keysvc_server.py        # Task 1（Modify：handle_address + 私鑰不外洩）
├── test_keysvc_client.py        # Task 1（Modify：address 往返 + KeysvcError.code）
├── publicapi_helpers.py         # Task 7：共用 fake（FakeKeysvc/FakeHL）、make_cfg、login
├── test_publicapi_config.py     # Task 2
├── test_publicapi_siwe.py       # Task 3
├── test_publicapi_store.py     # Task 4
├── test_publicapi_approvals.py  # Task 5
├── test_publicapi_hl.py         # Task 6
├── test_api_auth.py             # Task 7
├── test_api_onboard.py          # Task 8
├── test_api_payload.py          # Task 9
├── test_api_verify_admin.py     # Task 10
├── test_filet_activate.py       # Task 11
└── test_publicapi_integration.py# Task 13：離線端到端 + 非託管不變量 + desync 契約
```

## 模型分工與 review gate

| Task | 主題 | 實作 | 驗收 | 加驗 |
|---|---|---|---|---|
| 0 | 分支+依賴+基線 | haiku | sonnet read-back | — |
| 1 | keysvc 唯讀 address op + error code ⭐ | sonnet | sonnet fresh | ⭐ keysvc 紅線（私鑰仍不出進程；address op 碰 keystore 讀取路徑）|
| 2 | config/identity | sonnet | sonnet fresh | — |
| 3 | SIWE 驗簽 ⭐ | sonnet | sonnet fresh | ⭐ 紅線 4（權威重建、不解析自由文本）|
| 4 | ApiStore ⭐ | sonnet | sonnet fresh | ⭐ 紅線 4（nonce 原子單次使用）|
| 5 | typed-data builder ⭐ | sonnet | sonnet fresh | ⭐ research 對照（動態 chainId、agentName、SDK pin）|
| 6 | HLGateway（唯讀） | sonnet | sonnet fresh | 工程原則 2/5（讀=冪等重試、單一邊界、httpx 例外轉譯）|
| 7 | auth 端點 + session ⭐ | sonnet | sonnet fresh | ⭐ 紅線 3/4（cookie 屬性、session 綁定）|
| 8 | agent + status 端點 | sonnet | sonnet fresh | —（desync 自癒分支含在驗收）|
| 9 | payload 端點 ⭐ | sonnet | sonnet fresh | ⭐ 紅線 5/6（typed data 錯 = 使用者簽錯東西；builder/agent 欄位出自伺服器）|
| 10 | verify + pending + admin | sonnet | sonnet fresh | ⭐ 紅線 6（builder 常數、user 綁 session）|
| 11 | activate CLI ⭐ | sonnet | sonnet fresh | ⭐ 紅線 6（builder 結構性核對）|
| 12 | 入口 + systemd | haiku | sonnet read-back | — |
| 13 | 端到端 ⭐ | sonnet | sonnet fresh + **opus 第二意見** | ⭐ 非託管不變量整條驗 + desync 契約 |

- 每任務：實作 → fresh-context 驗收 → commit。全部 commit 落 `feat/m2-publicapi`（自 `feat/m2-keyservice` 分出）。不 push、不動 main。
- 所有 commit 帶 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` footer。

---

### Task 0: 分支、依賴、基線

- [ ] **Step 1** `git checkout -b feat/m2-publicapi feat/m2-keyservice`；`git branch --show-current` 應為 `feat/m2-publicapi`。
- [ ] **Step 2** `uv run pytest -q` → 基線 `570 passed, 2 deselected`（keyservice 計畫收尾狀態）；`uv run ruff check src tests scripts` 乾淨。不符則停下回報。
- [ ] **Step 3** 加依賴：`uv add fastapi uvicorn httpx`（httpx 同時供 `HLGateway` 與 TestClient 用）。
- [ ] **Step 4** 再跑 `uv run pytest -q` 確認依賴變更不影響基線。
- [ ] **Step 5** `git add pyproject.toml uv.lock && git commit -m "chore: add fastapi/uvicorn/httpx for public API"`。

---

### Task 1: keysvc 唯讀 address op + 結構化 error code ⭐

**Files:** Modify `src/spark/keysvc/protocol.py`、`src/spark/keysvc/server.py`、`src/spark/keysvc/client.py`；Modify `tests/test_keysvc_protocol.py`、`tests/test_keysvc_server.py`、`tests/test_keysvc_client.py`。
先讀三個 keysvc 原始檔與 `src/spark/keystore/envfile.py`（確認 `get_agent_signer` 對缺檔的行為——open 缺檔 → `FileNotFoundError`；若實際型別不同，`handle_address` 的 except 對應調整）。

**這是 spec deviation（設計定案 12）**：spec 寫 key-service「無其他操作」，其意圖是「不讀鑰、不簽名、最小攻擊面」；`address` op 只回 agent **地址**（公開資訊，本來就出現在 generate 回應與鏈上），不違反意圖，且把「API DB 遺失 → 使用者永久卡死（generate 拒重生、地址拿不回）」變成自動復原（Task 8 自癒）。同步把 `Response` 加結構化 `code`，消滅跨進程的中文訊息子字串比對（opus 審 M3）。**紅線不變：私鑰仍不出 keysvc 進程**——`address` 只回 `signer.address`，不回、不 log 任何鑰。

- [ ] **Step 1: 失敗測試（protocol，加到 `tests/test_keysvc_protocol.py`）**

```python
from spark.keysvc.protocol import AddressRequest  # 檔頭既有 import 區補


def test_address_request_roundtrip():
    line = encode_request(AddressRequest(account_id="alice"))
    assert line.endswith(b"\n")
    req = decode_request(line)
    assert isinstance(req, AddressRequest) and req.account_id == "alice"


def test_generate_request_type_preserved():
    req = decode_request(encode_request(GenerateRequest(account_id="alice")))
    assert isinstance(req, GenerateRequest)


def test_response_code_roundtrip():
    resp = decode_response(encode_response(Response(ok=False, error="x", code="exists")))
    assert resp.ok is False and resp.error == "x" and resp.code == "exists"


def test_response_code_absent_is_none():
    resp = decode_response(encode_response(Response(ok=True, agent_address="0x" + "a" * 40)))
    assert resp.code is None
```

- [ ] **Step 2** 跑到失敗（ImportError/AttributeError）。既有 protocol 測試必須維持綠（只加欄位與 op，不改既有訊息形狀）。
- [ ] **Step 3: 實作 `protocol.py`**（在既有內容上修改；`Response` 只加選欄）

```python
@dataclass(frozen=True)
class AddressRequest:
    account_id: str


@dataclass(frozen=True)
class Response:
    ok: bool
    agent_address: str | None = None
    error: str | None = None
    code: str | None = None  # 結構化錯誤碼："exists"|"invalid"|"missing"|"internal"；成功 None


def encode_request(req: GenerateRequest | AddressRequest) -> bytes:
    op = "generate" if isinstance(req, GenerateRequest) else "address"
    return (json.dumps({"op": op, "account_id": req.account_id}) + "\n").encode()


def decode_request(line: bytes) -> GenerateRequest | AddressRequest:
    d = json.loads(line.decode())
    if not isinstance(d, dict):
        raise ValueError("request 須為 JSON object")
    acct = d.get("account_id")
    if not acct:
        raise ValueError("missing account_id")
    op = d.get("op")
    if op == "generate":
        return GenerateRequest(account_id=acct)
    if op == "address":
        return AddressRequest(account_id=acct)
    raise ValueError(f"unsupported op: {op!r}")


def encode_response(resp: Response) -> bytes:
    body = {"ok": resp.ok}
    if resp.agent_address is not None:
        body["agent_address"] = resp.agent_address
    if resp.error is not None:
        body["error"] = resp.error
    if resp.code is not None:
        body["code"] = resp.code
    return (json.dumps(body) + "\n").encode()


def decode_response(line: bytes) -> Response:
    d = json.loads(line.decode())
    return Response(ok=bool(d.get("ok")), agent_address=d.get("agent_address"),
                    error=d.get("error"), code=d.get("code"))
```

（若現行檔的 `decode_request`/`decode_response` 有本計畫未列的防禦分支——如 isinstance dict 檢查——保留之，只疊加上述變更。）

- [ ] **Step 4: 失敗測試（server，加到 `tests/test_keysvc_server.py`）**

```python
from spark.keysvc.protocol import AddressRequest  # 檔頭補
from spark.keysvc.server import handle_address    # 檔頭補


def test_generate_error_codes(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    handle_generate(GenerateRequest("alice"), ks)
    assert handle_generate(GenerateRequest("alice"), ks).code == "exists"
    assert handle_generate(GenerateRequest("../evil"), ks).code == "invalid"


def test_address_returns_existing_agent_address(tmp_path):
    ks = EnvFileKeyStore(tmp_path)
    gen = handle_generate(GenerateRequest("alice"), ks)
    resp = handle_address(AddressRequest("alice"), ks)
    assert resp.ok and resp.agent_address == gen.agent_address and resp.code is None


def test_address_missing_key_code(tmp_path):
    resp = handle_address(AddressRequest("alice"), EnvFileKeyStore(tmp_path))
    assert resp.ok is False and resp.code == "missing"


def test_address_bad_account_id_code(tmp_path):
    resp = handle_address(AddressRequest("../evil"), EnvFileKeyStore(tmp_path))
    assert resp.ok is False and resp.code == "invalid"


def test_address_private_key_never_in_response(tmp_path):
    """⭐ 紅線同 generate：address op 讀 keystore，但私鑰不進回應任何欄位。"""
    ks = EnvFileKeyStore(tmp_path)
    handle_generate(GenerateRequest("alice"), ks)
    resp = handle_address(AddressRequest("alice"), ks)
    pk = (tmp_path / "alice" / "agent.key").read_text().strip()
    blob = f"{resp.ok}{resp.agent_address}{resp.error}{resp.code}"
    assert pk not in blob and pk.removeprefix("0x") not in blob
```

- [ ] **Step 5: 實作 `server.py`**——`handle_generate` 三個 except 分支補 `code=`（`FileExistsError`→`"exists"`、`ValueError`→`"invalid"`、`Exception`→`"internal"`，訊息欄位不變）；新增：

```python
from spark.filet.followers import validate_account_id  # 檔頭補（envfile 同源）
from spark.keysvc.protocol import AddressRequest       # import 區補


def handle_address(req: AddressRequest, ks: EnvFileKeyStore) -> Response:
    """唯讀：回既有 agent 的地址（desync 自癒用，設計定案 12）。
    私鑰只在 signer 區域變數——不進回應、不進 log（紅線同 generate）。"""
    try:
        validate_account_id(req.account_id)
        signer = ks.get_agent_signer(req.account_id)
    except ValueError as e:
        return Response(ok=False, error=str(e), code="invalid")
    except FileNotFoundError:
        return Response(ok=False, error=f"account {req.account_id} 無 agent key",
                        code="missing")
    except Exception:  # noqa: BLE001 — 不外洩細節（可能含路徑，不含私鑰）
        logger.exception("keysvc address 失敗 account=%s", req.account_id)
        return Response(ok=False, error="internal error", code="internal")
    return Response(ok=True, agent_address=signer.address)
```

serve 迴圈的分派（原 `resp = handle_generate(req, ks)` 一行改為）：

```python
                    if isinstance(req, GenerateRequest):
                        resp = handle_generate(req, ks)
                    else:
                        resp = handle_address(req, ks)
```

- [ ] **Step 6: 失敗測試（client，加到 `tests/test_keysvc_client.py`，沿檔內 socket-ban 旁路慣例）**

```python
from spark.keysvc.client import KeysvcError  # 檔頭補


def test_client_address_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)
    sock_path = Path(f"/tmp/spark-keysvc-cli-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    t, stop = _start_server(sock_path, ks)
    try:
        client = KeysvcClient(str(sock_path))
        addr = client.generate("alice")
        assert client.address("alice") == addr
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)


def test_client_address_missing_raises_with_code(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)
    sock_path = Path(f"/tmp/spark-keysvc-cli-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    t, stop = _start_server(sock_path, ks)
    try:
        with pytest.raises(KeysvcError) as ei:
            KeysvcClient(str(sock_path)).address("ghost")
        assert ei.value.code == "missing"
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)


def test_client_generate_exists_code(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET_CTOR)
    sock_path = Path(f"/tmp/spark-keysvc-cli-test-{uuid.uuid4().hex[:8]}.sock")
    ks = EnvFileKeyStore(tmp_path / "keys")
    t, stop = _start_server(sock_path, ks)
    try:
        client = KeysvcClient(str(sock_path))
        client.generate("alice")
        with pytest.raises(KeysvcError) as ei:
            client.generate("alice")
        assert ei.value.code == "exists"
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)
```

- [ ] **Step 7: 實作 `client.py`**

```python
"""src/spark/keysvc/client.py
public API 用來呼叫 key-service 的 client：generate（寫）與 address（唯讀）。"""
import socket

from spark.keysvc.protocol import (AddressRequest, GenerateRequest,
                                   decode_response, encode_request)


class KeysvcError(RuntimeError):
    """keysvc 回 ok=False 時拋出。code 供呼叫端結構化分支（"exists"/"invalid"/
    "missing"/"internal"/None）——不得比對訊息字串。繼承 RuntimeError：既有
    `pytest.raises(RuntimeError)` 測試維持綠。"""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class KeysvcClient:
    def __init__(self, sock_path: str):
        self._sock_path = sock_path

    def _call(self, req) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(self._sock_path)
            c.sendall(encode_request(req))
            line = c.makefile("rb").readline()
        resp = decode_response(line)
        if not resp.ok:
            raise KeysvcError(f"keysvc 失敗: {resp.error}", code=resp.code)
        return resp.agent_address

    def generate(self, account_id: str) -> str:
        """生成 agent、回地址。失敗 raise KeysvcError（含 code；不含私鑰）。"""
        return self._call(GenerateRequest(account_id))

    def address(self, account_id: str) -> str:
        """唯讀：查既有 agent 地址（desync 自癒用）。無 key → KeysvcError(code="missing")。"""
        return self._call(AddressRequest(account_id))
```

- [ ] **Step 8** `uv run pytest tests/test_keysvc_protocol.py tests/test_keysvc_server.py tests/test_keysvc_client.py tests/test_keysvc_integration.py -q` 全綠（既有 keysvc 測試不得紅）+ ruff。
- [ ] **Step 9** `git commit -m "feat: keysvc read-only address op + structured error codes (desync self-heal)"`。

---

### Task 2: config/identity（normalize_address、derive_account_id、ApiConfig）

**Files:** Create `src/spark/publicapi/__init__.py`、`src/spark/publicapi/config.py`、`tests/test_publicapi_config.py`。
先讀 `src/spark/filet/followers.py`（validate_account_id 單一真相）、`src/spark/config.py`（API_URLS）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_publicapi_config.py"""
import pytest

from spark.filet.followers import validate_account_id
from spark.publicapi.config import ApiConfig, derive_account_id, normalize_address

_ADDR = "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12"


def test_normalize_address_lowercases():
    assert normalize_address(_ADDR) == _ADDR.lower()
    assert normalize_address(_ADDR.lower()) == _ADDR.lower()


def test_normalize_address_rejects_bad():
    for bad in ["", "0x123", "abc", _ADDR[2:], "0x" + "g" * 40, None]:
        with pytest.raises((ValueError, TypeError)):
            normalize_address(bad)


def test_derive_account_id_full_40hex():
    acct = derive_account_id(_ADDR)
    assert acct == "f" + _ADDR[2:].lower()
    assert len(acct) == 41
    validate_account_id(acct)  # 恆為引擎合法 account_id


def test_derive_account_id_deterministic_case_insensitive():
    assert derive_account_id(_ADDR) == derive_account_id(_ADDR.lower())


def _env(**over):
    base = {
        "FILET_API_NETWORK": "testnet",
        "FILET_BUILDER_ADDR": "0x" + "b1" * 20,
        "FILET_SIWE_DOMAIN": "filet.example",
        "FILET_SIWE_URI": "https://filet.example",
        "FILET_API_DB": "/tmp/api.db",
        "FILET_KEYSVC_SOCK": "/run/filet/keysvc.sock",
        "FILET_PENDING_PATH": "/tmp/pending.json",
        "FILET_ADMIN_ADDRESSES": "0x" + "ad" * 20,
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_from_env_builds_config():
    cfg = ApiConfig.from_env(_env())
    assert cfg.network == "testnet"
    assert cfg.builder_address == "0x" + "b1" * 20
    assert cfg.is_mainnet is False
    assert cfg.api_url == "https://api.hyperliquid-testnet.xyz"
    assert cfg.admin_addresses == frozenset({"0x" + "ad" * 20})
    assert cfg.agent_name == "filet"
    assert cfg.max_fee_rate == "0.1%"


def test_from_env_missing_var_raises():
    with pytest.raises(ValueError, match="FILET_BUILDER_ADDR"):
        ApiConfig.from_env(_env(FILET_BUILDER_ADDR=None))


def test_from_env_bad_network_raises():
    with pytest.raises(ValueError, match="network"):
        ApiConfig.from_env(_env(FILET_API_NETWORK="devnet"))


def test_admin_addresses_optional_and_normalized():
    cfg = ApiConfig.from_env(_env(FILET_ADMIN_ADDRESSES=None))
    assert cfg.admin_addresses == frozenset()
    cfg2 = ApiConfig.from_env(_env(FILET_ADMIN_ADDRESSES="0x" + "AD" * 20))
    assert cfg2.admin_addresses == frozenset({"0x" + "ad" * 20})


def test_constants_single_source():
    """opus 審 M4：門檻與費率上限不重新宣告字面量，直接引用 spark.config 既有常數。"""
    from spark.config import MIN_BUILDER_BALANCE, Settings
    cfg = ApiConfig.from_env(_env())
    assert cfg.max_fee_rate == Settings.max_rate
    assert cfg.min_user_deposit is MIN_BUILDER_BALANCE
    assert cfg.min_builder_balance is MIN_BUILDER_BALANCE
```

- [ ] **Step 2** `uv run pytest tests/test_publicapi_config.py -q` → FAIL（ImportError）。
- [ ] **Step 3: 實作**

`src/spark/publicapi/__init__.py`：

```python
"""Public API（M2 onboarding 後端）：SIWE 登入、產待簽 payload（前端簽完直送 HL）、
verify（鏈上查詢）、admin 唯讀。非託管不變量：主鑰與 EIP-712 授權簽名永不進本套件
任何路徑；agent 私鑰只在 key-service。"""
```

`src/spark/publicapi/config.py`：

```python
"""src/spark/publicapi/config.py
Public API 設定與身分衍生。
- normalize_address：所有地址比對的單一基準（0x + 40 hex 小寫）——SIWE recover vs
  nonce 綁定地址、agent vs extraAgents、admin 白名單、builder 核對，一律先過這裡
  （工程原則 1：同基準比較）。
- derive_account_id：spec 資料模型定死——"f" + 地址小寫去 0x 完整 40 hex（41 字元、
  1:1 不截斷、恆過 validate_account_id，無使用者輸入、無路徑穿越）。"""
import os
from dataclasses import dataclass
from decimal import Decimal

from spark.config import API_URLS, MIN_BUILDER_BALANCE, Settings
from spark.filet.followers import validate_account_id

_HEX = set("0123456789abcdefABCDEF")


def normalize_address(addr: str) -> str:
    if not (isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42
            and all(c in _HEX for c in addr[2:])):
        raise ValueError(f"不是合法地址（0x + 40 hex）: {addr!r}")
    return addr.lower()


def derive_account_id(user_address: str) -> str:
    acct = "f" + normalize_address(user_address)[2:]
    validate_account_id(acct)  # 縱深防禦（結構上必過；單一真相沿 followers.py）
    return acct


@dataclass(frozen=True)
class ApiConfig:
    network: str                      # testnet | mainnet
    builder_address: str              # 伺服器常數，絕非使用者輸入（spec opus 審查 m3）
    siwe_domain: str                  # SIWE 訊息綁 domain/URI（防跨站釣魚重放）
    siwe_uri: str
    db_path: str
    keysvc_sock: str
    pending_path: str
    admin_addresses: frozenset[str]   # normalize 過的管理員地址白名單
    agent_name: str = "filet"         # research：一律給名字，避開 SDK 空名刪欄位特例
    # 常數單一來源（opus 審 M4）：不重新宣告字面量，直接引用 spark.config 既有常數。
    # max_rate 無模組級常數——dataclass 的純預設值即類屬性，Settings.max_rate == "0.1%"（D6）。
    max_fee_rate: str = Settings.max_rate
    # 兩種語意共用同一鏈上門檻常數（builder 啟用門檻 100 USDC），兩個別名指向同一來源：
    min_user_deposit: Decimal = MIN_BUILDER_BALANCE     # 使用者入金門檻（status/verify funded）
    min_builder_balance: Decimal = MIN_BUILDER_BALANCE  # builder 資格門檻（payload pre-flight）
    session_ttl_s: int = 7 * 24 * 3600
    nonce_ttl_s: int = 300

    @property
    def is_mainnet(self) -> bool:
        return self.network == "mainnet"

    @property
    def api_url(self) -> str:
        return API_URLS[self.network]

    @classmethod
    def from_env(cls, env=None) -> "ApiConfig":
        env = os.environ if env is None else env
        required = ["FILET_API_NETWORK", "FILET_BUILDER_ADDR", "FILET_SIWE_DOMAIN",
                    "FILET_SIWE_URI", "FILET_API_DB", "FILET_KEYSVC_SOCK",
                    "FILET_PENDING_PATH"]
        missing = [k for k in required if not env.get(k)]
        if missing:
            raise ValueError(f"缺少環境變數: {', '.join(missing)}")
        network = env["FILET_API_NETWORK"]
        if network not in API_URLS:
            raise ValueError(f"unknown network: {network}")
        admins = frozenset(normalize_address(a.strip())
                           for a in env.get("FILET_ADMIN_ADDRESSES", "").split(",")
                           if a.strip())
        return cls(network=network,
                   builder_address=normalize_address(env["FILET_BUILDER_ADDR"]),
                   siwe_domain=env["FILET_SIWE_DOMAIN"],
                   siwe_uri=env["FILET_SIWE_URI"],
                   db_path=env["FILET_API_DB"],
                   keysvc_sock=env["FILET_KEYSVC_SOCK"],
                   pending_path=env["FILET_PENDING_PATH"],
                   admin_addresses=admins)
```

- [ ] **Step 4** `uv run pytest tests/test_publicapi_config.py -q` 全綠 + `uv run ruff check src/spark/publicapi tests/test_publicapi_config.py`。
- [ ] **Step 5** `git commit -m "feat: public API config + identity derivation (normalize_address, f+40hex account_id)"`。

---

### Task 3: SIWE（EIP-4361 訊息重建 + 驗簽）⭐

**Files:** Create `src/spark/publicapi/siwe.py`、`tests/test_publicapi_siwe.py`。

設計：**伺服器權威重建、不解析自由文本**——domain/URI 出自設定、nonce/issued_at 出自伺服器儲存，nonce 端點把完整 message 回給前端照簽；verify 時伺服器用儲存的欄位重建同一字串再 recover。前端動不了 domain/URI（動了驗簽必失敗），「綁 domain/URI」因此是結構保證，且零解析攻擊面。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_publicapi_siwe.py
SIWE 用真密碼學（eth_account 本地運算，不觸網）。"""
from eth_account import Account
from eth_account.messages import encode_defunct

from spark.publicapi.config import normalize_address
from spark.publicapi.siwe import build_siwe_message, recover_siwe_signer

_NONCE = "abcd1234deadbeef" * 2
_ISSUED = "2026-07-17T00:00:00Z"


def _msg(addr, domain="filet.example"):
    return build_siwe_message(domain=domain, uri="https://filet.example",
                              address=addr, chain_id=42161,
                              nonce=_NONCE, issued_at=_ISSUED)


def test_siwe_roundtrip_recovers_signer():
    acct = Account.create()
    msg = _msg(acct.address)
    sig = acct.sign_message(encode_defunct(text=msg)).signature.hex()
    assert normalize_address(recover_siwe_signer(msg, sig)) == acct.address.lower()


def test_siwe_wrong_signer_detected():
    a, b = Account.create(), Account.create()
    msg = _msg(a.address)
    sig = b.sign_message(encode_defunct(text=msg)).signature.hex()
    assert normalize_address(recover_siwe_signer(msg, sig)) != a.address.lower()


def test_siwe_message_eip4361_shape():
    m = _msg("0x" + "ab" * 20)
    assert m.startswith(
        "filet.example wants you to sign in with your Ethereum account:\n")
    for line in ("URI: https://filet.example", "Version: 1", "Chain ID: 42161",
                 f"Nonce: {_NONCE}", f"Issued At: {_ISSUED}"):
        assert line in m


def test_siwe_message_binds_domain():
    """不同 domain → 不同訊息 → 他站簽名在本站必然驗不過（防跨站釣魚重放）。"""
    assert _msg("0x" + "ab" * 20) != _msg("0x" + "ab" * 20, domain="evil.example")


def test_siwe_message_uses_checksum_address():
    acct = Account.create()
    assert f"\n{acct.address}\n" in _msg(acct.address.lower())
```

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作**

```python
"""src/spark/publicapi/siwe.py
EIP-4361（SIWE）：伺服器權威重建訊息 + eth_account 驗簽（EIP-191 personal_sign）。
刻意不解析前端送來的自由文本：domain/URI 出自伺服器設定、nonce/issued_at 出自伺服器
儲存，前端只能簽「伺服器重建得出來的訊息」——綁 domain/URI 因此是結構保證。"""
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address


def build_siwe_message(*, domain: str, uri: str, address: str, chain_id: int,
                       nonce: str, issued_at: str) -> str:
    """EIP-4361 標準版型（Version 1）；address 以 EIP-55 checksum 呈現。"""
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{to_checksum_address(address)}\n"
        "\n"
        "Sign in to Filet.\n"
        "\n"
        f"URI: {uri}\n"
        "Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )


def recover_siwe_signer(message: str, signature: str) -> str:
    """personal_sign recover；壞簽名拋例外（eth_account 系），呼叫端轉 401。"""
    return Account.recover_message(encode_defunct(text=message), signature=signature)
```

- [ ] **Step 4** 全綠 + ruff。
- [ ] **Step 5** `git commit -m "feat: SIWE message build + signature recovery (server-authoritative EIP-4361)"`。

---

### Task 4: ApiStore（SQLite：nonce 單次使用、session、onboarding 進度）⭐

**Files:** Create `src/spark/publicapi/store.py`、`tests/test_publicapi_store.py`。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_publicapi_store.py"""
from spark.publicapi.store import ApiStore

_ADDR = "0x" + "ab" * 20


def _store(tmp_path):
    return ApiStore(tmp_path / "api.db")


def test_nonce_single_use(tmp_path):
    st = _store(tmp_path)
    n = st.issue_nonce(_ADDR, 42161, "2026-07-17T00:00:00Z", now_s=1000.0, ttl_s=300)
    rec = st.consume_nonce(n, now_s=1001.0)
    assert rec is not None
    assert rec.address == _ADDR and rec.chain_id == 42161
    assert rec.issued_at == "2026-07-17T00:00:00Z"
    # 第二次一定 None：原子 UPDATE 單次使用（防有效期內重放）
    assert st.consume_nonce(n, now_s=1002.0) is None


def test_nonce_expired_not_consumable(tmp_path):
    st = _store(tmp_path)
    n = st.issue_nonce(_ADDR, 1, "2026-07-17T00:00:00Z", now_s=1000.0, ttl_s=300)
    assert st.consume_nonce(n, now_s=1301.0) is None


def test_nonce_unknown_none(tmp_path):
    assert _store(tmp_path).consume_nonce("nope", now_s=0.0) is None


def test_session_roundtrip_expiry_delete(tmp_path):
    st = _store(tmp_path)
    sid = st.create_session(_ADDR, now_s=1000.0, ttl_s=3600)
    assert st.get_session_address(sid, now_s=2000.0) == _ADDR
    assert st.get_session_address(sid, now_s=4601.0) is None      # 過期
    assert st.get_session_address("nope", now_s=1000.0) is None   # 不存在
    st.delete_session(sid)
    assert st.get_session_address(sid, now_s=1001.0) is None


def test_onboarding_agent_address(tmp_path):
    st = _store(tmp_path)
    acct = "f" + "ab" * 20
    assert st.get_agent_address(acct) is None
    st.ensure_onboarding(acct, _ADDR)
    st.ensure_onboarding(acct, _ADDR)  # 冪等
    assert st.get_agent_address(acct) is None
    st.set_agent_address(acct, "0x" + "cd" * 20)
    assert st.get_agent_address(acct) == "0x" + "cd" * 20


def test_onboarding_rows_isolated(tmp_path):
    st = _store(tmp_path)
    a, b = "f" + "ab" * 20, "f" + "cd" * 20
    st.ensure_onboarding(a, _ADDR)
    st.ensure_onboarding(b, "0x" + "cd" * 20)
    st.set_agent_address(a, "0x" + "ee" * 20)
    assert st.get_agent_address(b) is None  # 各 account 進度獨立
```

（注意：本表**刻意不存任何 action/typed data/簽名**——前端持有 typed data、簽完直送
HL（設計定案 1），後端無提交路徑，DB 只需地址與進度。）

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作**

```python
"""src/spark/publicapi/store.py
API 狀態落地（SQLite 單檔，spec 資料模型）：SIWE nonce（單次使用）、session、
onboarding 進度。金鑰/簽名/typed data 一律不落地——前端持有 typed data、簽完
直送 HL（設計定案 1），本表只存地址與進度。"""
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nonces (
    nonce TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    chain_id INTEGER NOT NULL,
    issued_at TEXT NOT NULL,
    expiry REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    expiry REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS onboarding (
    account_id TEXT PRIMARY KEY,
    user_address TEXT NOT NULL,
    agent_address TEXT
);
"""


@dataclass(frozen=True)
class NonceRecord:
    address: str
    chain_id: int
    issued_at: str


class ApiStore:
    """單一連線 + lock（FastAPI handler 跑 threadpool，需 thread-safe）。"""

    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    # --- SIWE nonce（單次使用） ---
    def issue_nonce(self, address: str, chain_id: int, issued_at: str,
                    *, now_s: float, ttl_s: int) -> str:
        nonce = secrets.token_hex(16)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO nonces (nonce, address, chain_id, issued_at, expiry) "
                "VALUES (?, ?, ?, ?, ?)",
                (nonce, address, chain_id, issued_at, now_s + ttl_s))
        return nonce

    def consume_nonce(self, nonce: str, *, now_s: float) -> NonceRecord | None:
        """單次使用的結構性保證：原子 UPDATE consumed 0→1，rowcount != 1 即
        「不存在／已用過／已過期」一律 None——不是先查再改的 TOCTOU。"""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE nonces SET consumed = 1 "
                "WHERE nonce = ? AND consumed = 0 AND expiry > ?", (nonce, now_s))
            if cur.rowcount != 1:
                return None
            row = self._db.execute(
                "SELECT address, chain_id, issued_at FROM nonces WHERE nonce = ?",
                (nonce,)).fetchone()
        return NonceRecord(address=row[0], chain_id=row[1], issued_at=row[2])

    # --- session ---
    def create_session(self, address: str, *, now_s: float, ttl_s: int) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO sessions (session_id, address, expiry) VALUES (?, ?, ?)",
                (sid, address, now_s + ttl_s))
        return sid

    def get_session_address(self, session_id: str, *, now_s: float) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT address FROM sessions WHERE session_id = ? AND expiry > ?",
                (session_id, now_s)).fetchone()
        return row[0] if row else None

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # --- onboarding 進度 ---
    def ensure_onboarding(self, account_id: str, user_address: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO onboarding (account_id, user_address) VALUES (?, ?)",
                (account_id, user_address))

    def get_agent_address(self, account_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT agent_address FROM onboarding WHERE account_id = ?",
                (account_id,)).fetchone()
        return row[0] if row else None

    def set_agent_address(self, account_id: str, agent_address: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE onboarding SET agent_address = ? WHERE account_id = ?",
                             (agent_address, account_id))
```

- [ ] **Step 4** 全綠 + ruff。
- [ ] **Step 5** `git commit -m "feat: API store (SQLite) — atomic single-use SIWE nonce, sessions, onboarding progress"`。

---

### Task 5: ApproveAgent / ApproveBuilderFee typed-data builder ⭐

**Files:** Create `src/spark/publicapi/approvals.py`、`tests/test_publicapi_approvals.py`。
先讀 research §1/§6（typed data 結構、`user_signed_payload` 無私鑰）與 `.venv/lib/python3.14/site-packages/hyperliquid/utils/signing.py:217-237, 410-438`（sign types 原文）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_publicapi_approvals.py
typed-data builder：無私鑰建構、動態 chainId、agentName 一律給、SDK pin round-trip。
簽名/recover 全為本地密碼學運算，不觸網。"""
import time

from eth_account import Account
from eth_account.messages import encode_typed_data
from hyperliquid.utils.signing import recover_user_from_user_signed_action

from spark.publicapi.approvals import (
    APPROVE_AGENT_PRIMARY, APPROVE_AGENT_SIGN_TYPES,
    APPROVE_BUILDER_FEE_PRIMARY, APPROVE_BUILDER_FEE_SIGN_TYPES,
    build_approve_agent, build_approve_builder_fee)

_AGENT = "0x" + "ab" * 20
_BUILDER = "0x" + "cd" * 20


def test_approve_agent_typed_data_shape():
    td, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                     wallet_chain_id=0xA4B1, is_mainnet=False,
                                     nonce_ms=1234)
    assert td["domain"] == {"name": "HyperliquidSignTransaction", "version": "1",
                            "chainId": 0xA4B1,
                            "verifyingContract": "0x" + "0" * 40}
    assert td["primaryType"] == APPROVE_AGENT_PRIMARY
    assert td["message"] == action
    assert action["type"] == "approveAgent"
    assert action["hyperliquidChain"] == "Testnet"
    assert action["signatureChainId"] == "0xa4b1"
    assert action["agentAddress"] == _AGENT
    assert action["agentName"] == "filet"   # 一律給名字（research：避開 SDK 空名刪欄位特例）
    assert action["nonce"] == 1234


def test_domain_chain_id_follows_wallet():
    """research 風險 1：signatureChainId 動態取自前端錢包，不硬編 0x66eee。"""
    td, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                     wallet_chain_id=1, is_mainnet=True, nonce_ms=1)
    assert td["domain"]["chainId"] == 1
    assert action["signatureChainId"] == "0x1"
    assert action["hyperliquidChain"] == "Mainnet"


def test_builder_fee_typed_data_shape():
    td, action = build_approve_builder_fee(builder=_BUILDER, max_fee_rate="0.1%",
                                           wallet_chain_id=0xA4B1, is_mainnet=True,
                                           nonce_ms=5)
    assert td["primaryType"] == APPROVE_BUILDER_FEE_PRIMARY
    assert action["type"] == "approveBuilderFee"
    assert action["maxFeeRate"] == "0.1%"
    assert action["builder"] == _BUILDER
    assert action["hyperliquidChain"] == "Mainnet"
    assert action["nonce"] == 5


def test_nonce_defaults_to_now_ms():
    _, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                    wallet_chain_id=1, is_mainnet=False)
    assert abs(action["nonce"] - time.time() * 1000) < 60_000


def _sign(wallet, typed_data):
    sm = wallet.sign_message(encode_typed_data(full_message=typed_data))
    return {"r": hex(sm.r), "s": hex(sm.s), "v": sm.v}


def test_sign_recover_roundtrip_pins_sdk_types_agent():
    """SDK pin 測試（research 風險 6）：本模組常數建的 typed data 簽出後，SDK
    recover 得回同一地址——SDK 升版若改 sign types，這裡先爆。"""
    wallet = Account.create()
    td, action = build_approve_agent(agent_address=_AGENT, agent_name="filet",
                                     wallet_chain_id=0xA4B1, is_mainnet=False,
                                     nonce_ms=1720000000000)
    rec = recover_user_from_user_signed_action(
        dict(action), _sign(wallet, td), APPROVE_AGENT_SIGN_TYPES,
        APPROVE_AGENT_PRIMARY, False)
    assert rec.lower() == wallet.address.lower()


def test_sign_recover_roundtrip_pins_sdk_types_builder_fee():
    wallet = Account.create()
    td, action = build_approve_builder_fee(builder=_BUILDER, max_fee_rate="0.1%",
                                           wallet_chain_id=0xA4B1, is_mainnet=False,
                                           nonce_ms=1720000000000)
    rec = recover_user_from_user_signed_action(
        dict(action), _sign(wallet, td), APPROVE_BUILDER_FEE_SIGN_TYPES,
        APPROVE_BUILDER_FEE_PRIMARY, False)
    assert rec.lower() == wallet.address.lower()
```

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作**

```python
"""src/spark/publicapi/approvals.py
ApproveAgent / ApproveBuilderFee 的 EIP-712 typed-data 建構（無私鑰）。
站在 SDK 現成的 user_signed_payload 上（signing.py:217-237），零手工 EIP-712 hash。
sign types 常數抄自 SDK signing.py:410-438；SDK 升版由 pin round-trip 測試守。
research: docs/superpowers/research/2026-07-17-hl-sdk-external-signing.md。"""
import time

from hyperliquid.utils.signing import user_signed_payload

APPROVE_AGENT_PRIMARY = "HyperliquidTransaction:ApproveAgent"
APPROVE_AGENT_SIGN_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "agentAddress", "type": "address"},
    {"name": "agentName", "type": "string"},
    {"name": "nonce", "type": "uint64"},
]

APPROVE_BUILDER_FEE_PRIMARY = "HyperliquidTransaction:ApproveBuilderFee"
APPROVE_BUILDER_FEE_SIGN_TYPES = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "maxFeeRate", "type": "string"},
    {"name": "builder", "type": "address"},
    {"name": "nonce", "type": "uint64"},
]


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_approve_agent(*, agent_address: str, agent_name: str, wallet_chain_id: int,
                        is_mainnet: bool, nonce_ms: int | None = None
                        ) -> tuple[dict, dict]:
    """回 (typed_data 給前端 eth_signTypedData_v4, action 存伺服器待提交)。無私鑰。
    signatureChainId = 前端錢包當下 chain（research 風險 1：MetaMask 強制
    domain.chainId == active chain）；hyperliquidChain 才決定環境與防重放。"""
    action = {
        "type": "approveAgent",
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "signatureChainId": hex(wallet_chain_id),
        "agentAddress": agent_address,
        "agentName": agent_name,
        "nonce": nonce_ms if nonce_ms is not None else _now_ms(),
    }
    return user_signed_payload(APPROVE_AGENT_PRIMARY, APPROVE_AGENT_SIGN_TYPES,
                               action), action


def build_approve_builder_fee(*, builder: str, max_fee_rate: str, wallet_chain_id: int,
                              is_mainnet: bool, nonce_ms: int | None = None
                              ) -> tuple[dict, dict]:
    action = {
        "type": "approveBuilderFee",
        "hyperliquidChain": "Mainnet" if is_mainnet else "Testnet",
        "signatureChainId": hex(wallet_chain_id),
        "maxFeeRate": max_fee_rate,
        "builder": builder,
        "nonce": nonce_ms if nonce_ms is not None else _now_ms(),
    }
    return user_signed_payload(APPROVE_BUILDER_FEE_PRIMARY,
                               APPROVE_BUILDER_FEE_SIGN_TYPES, action), action
```

- [ ] **Step 4** 全綠 + ruff。
- [ ] **Step 5** `git commit -m "feat: ApproveAgent/ApproveBuilderFee typed-data builders (keyless, dynamic chainId, SDK pin tests)"`。

---

### Task 6: HLGateway（HL 唯讀出口）

**Files:** Create `src/spark/publicapi/hl.py`、`tests/test_publicapi_hl.py`。
先讀 `src/spark/resilience.py`（run 的分類語意；沿用不改）。

**提交路徑註記（設計定案 1）**：前端拿 typed data 簽完後**直接 POST HL `/exchange`**（CORS 已實測全開），後端**沒有任何提交/寫入 HL 的程式路徑**——本 gateway 只有 `/info` 唯讀查詢（agent 已授權？builder fee 已核？入金與 builder 門檻？），全部冪等、transient 重試。v 正規化與 recover 預驗職責在前端計畫。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_publicapi_hl.py
HLGateway：唯讀查詢的解析與 transient 重試（用**真實 httpx 例外**驗轉譯——
內建 ConnectionError 驗重試是假信心，opus 審 I1）；結構性斷言後端無 /exchange 寫入面。
monkeypatch httpx.post，不觸網。"""
from decimal import Decimal

import httpx

from spark.publicapi.hl import HLGateway
from spark.resilience import RETRY_BASE_DELAY


class _Resp:
    """httpx.Response 替身：只要 raise_for_status/json 兩個面。"""

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakePost:
    def __init__(self, results):
        self.results = list(results)  # 每呼叫吐一個；Exception 則 raise
        self.calls = []

    def __call__(self, url, body):
        self.calls.append((url, body))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_gateway_reads_parse():
    post = _FakePost([
        {"marginSummary": {"accountValue": "123.45"}},
        50,
        [{"address": "0xAB" + "cd" * 19, "name": "filet"}],
    ])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.get_account_value("0x" + "ab" * 20) == Decimal("123.45")
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 50
    assert gw.agent_addresses("0x" + "ab" * 20) == [("0xAB" + "cd" * 19).lower()]
    assert all(u.endswith("/info") for u, _ in post.calls)


def test_default_post_translates_httpx_connect_error_and_retries(monkeypatch):
    """I1：httpx.ConnectError 不繼承內建 ConnectionError——經 _default_post 轉譯後
    resilience 才會分類為 transient 並重試。走真實 httpx 例外，斷言重試與 backoff。"""
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[Errno 61] Connection refused")
        return _Resp(7)

    monkeypatch.setattr(httpx, "post", fake_post)
    sleeps = []
    gw = HLGateway("https://x", sleep_fn=sleeps.append)  # post_fn 不注入 → 走 _default_post
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 7
    assert calls["n"] == 2
    assert sleeps == [RETRY_BASE_DELAY]  # 第一段 backoff 有被呼叫


def test_default_post_translates_empty_message_read_timeout(monkeypatch):
    """I1：httpx.ReadTimeout 訊息可為空字串——marker 比對救不了，靠型別轉譯。"""
    calls = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("")
        return _Resp(0)

    monkeypatch.setattr(httpx, "post", fake_post)
    gw = HLGateway("https://x", sleep_fn=lambda s: None)
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 0
    assert calls["n"] == 3


def test_gateway_read_retries_5xx_marker():
    """5xx 走 resilience 的訊息 marker 分類（httpx.HTTPStatusError 訊息含狀態碼字樣）。"""
    post = _FakePost([RuntimeError("Server error '503 Service Unavailable' for url"), 7])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    assert gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20) == 7
    assert len(post.calls) == 2


def test_gateway_has_no_exchange_write_surface():
    """紅線 5 的結構性斷言：gateway 沒有任何提交/寫入方法——前端直送 HL，
    後端連 /exchange 的呼叫路徑都不存在。"""
    gw = HLGateway("https://x", post_fn=lambda u, b: {}, sleep_fn=lambda s: None)
    assert not any("submit" in name or "exchange" in name
                   for name in dir(gw) if not name.startswith("__"))


def test_gateway_only_posts_to_info():
    post = _FakePost([{"marginSummary": {"accountValue": "1"}}, 0, []])
    gw = HLGateway("https://x", post_fn=post, sleep_fn=lambda s: None)
    gw.get_account_value("0x" + "ab" * 20)
    gw.max_builder_fee("0x" + "ab" * 20, "0x" + "cd" * 20)
    gw.agent_addresses("0x" + "ab" * 20)
    assert all(u == "https://x/info" for u, _ in post.calls)
```

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作 `hl.py`**

```python
"""src/spark/publicapi/hl.py
Public API 對 HL 的唯一出口（單一 resilience boundary，工程原則 5）——**唯讀**。
分類在呼叫點強制宣告（沿 spark.resilience.run）：讀取（/info）＝冪等 → transient 重試。
本模組刻意沒有任何 /exchange 提交路徑：已簽授權由前端直送 HL（設計定案 1），
後端結構上無法經手簽名（紅線 5，Task 13 有結構性測試）。"""
import time
from decimal import Decimal

import httpx

from spark.resilience import run

_TIMEOUT_S = 10.0


def _default_post(url: str, body: dict):
    """httpx 的 ConnectError/ReadTimeout 等**不繼承**內建 ConnectionError/TimeoutError，
    訊息還可能是空字串——`resilience._is_transient_error` 認不得，真實連線失敗會被
    誤分類成 semantic 直接上拋（opus 審 I1）。修法：在這個唯一的 IO 邊界把 httpx
    例外轉譯成分類器認得的內建型別（不動引擎共用的 resilience.py）。
    TimeoutException 是 TransportError 子類：先窄後寬。"""
    try:
        resp = httpx.post(url, json=body, timeout=_TIMEOUT_S)
    except httpx.TimeoutException as e:
        raise TimeoutError(str(e) or "hl info timed out") from e
    except httpx.TransportError as e:
        raise ConnectionError(f"hl info transport error: {e}") from e
    resp.raise_for_status()
    return resp.json()


class HLGateway:
    """post_fn / sleep_fn 可注入：測試給 fake post 與不真睡的 sleep（沿 resilience 慣例）。"""

    def __init__(self, base_url: str, post_fn=None, sleep_fn=time.sleep):
        self._base = base_url.rstrip("/")
        self._post = post_fn or _default_post
        self._sleep = sleep_fn

    def _info(self, body: dict, what: str):
        return run(lambda: self._post(f"{self._base}/info", body),
                   what=what, idempotent=True, sleep_fn=self._sleep)

    def get_account_value(self, address: str) -> Decimal:
        state = self._info({"type": "clearinghouseState", "user": address}, "HL 帳戶查詢")
        return Decimal(state["marginSummary"]["accountValue"])

    def max_builder_fee(self, user: str, builder: str) -> int:
        """使用者已核給 builder 的費率上限（十分之一 bp；0 = 未核）。verify/status 用
        != 0 判 builder fee approval 已上鏈；同時是 maxFeeRate 生效的鏈上真相。"""
        return int(self._info({"type": "maxBuilderFee", "user": user, "builder": builder},
                              "HL maxBuilderFee 查詢"))

    def agent_addresses(self, user: str) -> list[str]:
        """使用者已授權的 agent 地址清單（extraAgents）；小寫正規化供同基準比對。"""
        agents = self._info({"type": "extraAgents", "user": user}, "HL extraAgents 查詢")
        return [a["address"].lower() for a in agents if a.get("address")]
```

- [ ] **Step 4** 全綠 + ruff。
- [ ] **Step 5** `git commit -m "feat: read-only HL gateway (single resilience boundary, no exchange write surface)"`。

---

### Task 7: FastAPI app factory + auth 端點 + session ⭐

**Files:** Create `src/spark/publicapi/app.py`（本任務只含 auth 與 `/api/me`；onboard 端點在 Task 8/9/10 逐步加入）、`tests/publicapi_helpers.py`、`tests/test_api_auth.py`。
先讀 `tests/test_keysvc_client.py` 檔頭（socket-ban 旁路慣例——import 期捕捉真 socket、測試內 monkeypatch；**不修改 tests/conftest.py**）。

TestClient 注意事項（寫進測試檔，實作者照抄）：
- anyio 事件迴圈需要本機 socketpair（self-pipe，不出網）→ 沿 keysvc 慣例放行真 socket；HL/keysvc 全為注入 fake，測試內無可觸網路徑。
- session cookie 設 `secure=True` → httpx 不會在 http scheme 送出 → TestClient 一律用 `base_url="https://testserver"`。

- [ ] **Step 1: 共用測試工具 `tests/publicapi_helpers.py`**

```python
"""tests/publicapi_helpers.py — Public API 測試共用件（非測試檔）。
FakeKeysvc / FakeHL 是唯二的外部依賴替身；SIWE 與 EIP-712 簽名用真密碼學。"""
import secrets
from decimal import Decimal

from eth_account import Account
from eth_account.messages import encode_defunct

from spark.keysvc.client import KeysvcError
from spark.publicapi.app import create_app
from spark.publicapi.config import ApiConfig
from spark.publicapi.store import ApiStore

BUILDER = "0x" + "b1" * 20


def make_cfg(tmp_path, **over):
    base = dict(network="testnet", builder_address=BUILDER,
                siwe_domain="filet.example", siwe_uri="https://filet.example",
                db_path=str(tmp_path / "api.db"),
                keysvc_sock=str(tmp_path / "keysvc.sock"),
                pending_path=str(tmp_path / "pending.json"),
                admin_addresses=frozenset())
    base.update(over)
    return ApiConfig(**base)


class FakeKeysvc:
    """模擬 KeysvcClient：鏡像真 client 的 KeysvcError.code 行為（"exists"/"missing"），
    generate 一次成功、重呼 code="exists"（O_EXCL 語意）、address 唯讀；可注入失敗。"""

    def __init__(self):
        self.generated: dict[str, str] = {}
        self.fail: Exception | None = None          # generate 的注入失敗
        self.address_fail: Exception | None = None  # address 的注入失敗

    def generate(self, account_id: str) -> str:
        if self.fail is not None:
            raise self.fail
        if account_id in self.generated:
            raise KeysvcError(f"keysvc 失敗: account {account_id} 已有 agent key",
                              code="exists")
        addr = "0x" + secrets.token_hex(20)
        self.generated[account_id] = addr
        return addr

    def address(self, account_id: str) -> str:
        if self.address_fail is not None:
            raise self.address_fail
        if account_id not in self.generated:
            raise KeysvcError(f"keysvc 失敗: account {account_id} 無 agent key",
                              code="missing")
        return self.generated[account_id]


class FakeHL:
    """模擬 HLGateway（唯讀）；鏈上狀態由測試直接塞（模擬前端直送 HL 後授權上鏈）。
    鍵一律小寫（同 normalize 基準）。刻意與真 HLGateway 同面：無任何提交方法。"""

    def __init__(self):
        self.account_values: dict[str, Decimal] = {}
        self.max_fees: dict[tuple[str, str], int] = {}
        self.agents: dict[str, list[str]] = {}

    def get_account_value(self, address: str) -> Decimal:
        return self.account_values.get(address.lower(), Decimal("0"))

    def max_builder_fee(self, user: str, builder: str) -> int:
        return self.max_fees.get((user.lower(), builder.lower()), 0)

    def agent_addresses(self, user: str) -> list[str]:
        return [a.lower() for a in self.agents.get(user.lower(), [])]


def make_app(tmp_path, cfg=None):
    cfg = cfg or make_cfg(tmp_path)
    store = ApiStore(cfg.db_path)
    keysvc, hl = FakeKeysvc(), FakeHL()
    return create_app(cfg, store, keysvc, hl), cfg, store, keysvc, hl


def login(client, wallet=None):
    """完整 SIWE 登入（真密碼學）。session cookie 落在 client 的 cookie jar。"""
    wallet = wallet or Account.create()
    r = client.get("/api/auth/nonce",
                   params={"address": wallet.address, "chain_id": 42161})
    assert r.status_code == 200, r.text
    body = r.json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 200, r.text
    return wallet
```

- [ ] **Step 2: 失敗測試 `tests/test_api_auth.py`**

```python
"""tests/test_api_auth.py
SIWE 登入流程：nonce → 簽 → verify → session cookie。真密碼學、fake 外部依賴。"""
import socket

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient

from tests.publicapi_helpers import login, make_app, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉，早於 autouse 斷網 fixture（keysvc 慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    """TestClient 的 anyio 事件迴圈需本機 socketpair（self-pipe，不出網）。
    HL/keysvc 全為注入 fake——測試內無任何可觸網的程式路徑。"""
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")  # secure cookie 需 https scheme


def test_login_then_me(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    r = client.get("/api/me")
    assert r.status_code == 200
    assert r.json()["address"] == wallet.address.lower()
    assert r.json()["account_id"] == "f" + wallet.address.lower()[2:]


def test_me_without_session_401(tmp_path):
    app, *_ = make_app(tmp_path)
    assert _client(app).get("/api/me").status_code == 401


def test_verify_wrong_signer_401(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    a, b = Account.create(), Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": a.address, "chain_id": 1}).json()
    sig = b.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 401
    assert client.get("/api/me").status_code == 401


def test_garbage_signature_401(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    a = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": a.address, "chain_id": 1}).json()
    r = client.post("/api/auth/verify",
                    json={"nonce": body["nonce"], "signature": "0xdeadbeef"})
    assert r.status_code == 401


def test_nonce_single_use_replay_401(tmp_path):
    """⭐ nonce 單次使用：同一 nonce+有效簽名重放 → 401（防有效期內重放）。"""
    app, *_ = make_app(tmp_path)
    client = _client(app)
    wallet = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": wallet.address, "chain_id": 1}).json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    first = client.post("/api/auth/verify",
                        json={"nonce": body["nonce"], "signature": sig})
    assert first.status_code == 200
    replay = client.post("/api/auth/verify",
                         json={"nonce": body["nonce"], "signature": sig})
    assert replay.status_code == 401


def test_expired_nonce_401(tmp_path):
    cfg = make_cfg(tmp_path, nonce_ttl_s=0)  # 立即過期
    app, *_ = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    wallet = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": wallet.address, "chain_id": 1}).json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    assert r.status_code == 401


def test_session_cookie_attributes(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    # login 內的 verify 回應設 cookie；重打一次取原始 header 驗屬性
    wallet = Account.create()
    body = client.get("/api/auth/nonce",
                      params={"address": wallet.address, "chain_id": 1}).json()
    sig = wallet.sign_message(encode_defunct(text=body["message"])).signature.hex()
    r = client.post("/api/auth/verify", json={"nonce": body["nonce"], "signature": sig})
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie


def test_logout_clears_session(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.get("/api/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401


def test_nonce_bad_address_400(tmp_path):
    app, *_ = make_app(tmp_path)
    r = _client(app).get("/api/auth/nonce", params={"address": "nope", "chain_id": 1})
    assert r.status_code == 400
```

- [ ] **Step 3** 跑到失敗（ImportError）。
- [ ] **Step 4: 實作 `src/spark/publicapi/app.py`**（本任務版本：factory + auth + `/api/me`）

```python
"""src/spark/publicapi/app.py
FastAPI app factory。所有外部依賴（store / keysvc client / HL gateway / 時鐘）由
create_app 注入——測試全離線。onboarding 端點一律綁 session 地址：account_id 由
session 衍生，端點無 account 參數（紅線 3：別人不能替你 onboard 是結構保證）。"""
import logging
import time
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from spark.publicapi.config import ApiConfig, derive_account_id, normalize_address
from spark.publicapi.siwe import build_siwe_message, recover_siwe_signer
from spark.publicapi.store import ApiStore

logger = logging.getLogger(__name__)

SESSION_COOKIE = "filet_session"


class VerifyBody(BaseModel):
    nonce: str
    signature: str


def create_app(cfg: ApiConfig, store: ApiStore, keysvc, hl, now_fn=time.time) -> FastAPI:
    app = FastAPI(title="filet public api",
                  docs_url=None, redoc_url=None, openapi_url=None)

    def _require_session(request: Request) -> str:
        sid = request.cookies.get(SESSION_COOKIE)
        addr = store.get_session_address(sid, now_s=now_fn()) if sid else None
        if addr is None:
            raise HTTPException(status_code=401, detail="未登入或 session 已過期")
        return addr

    # ---------- auth ----------
    @app.get("/api/auth/nonce")
    def auth_nonce(address: str, chain_id: int):
        try:
            addr = normalize_address(address)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if chain_id <= 0:
            raise HTTPException(status_code=400, detail="chain_id 不合法")
        issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = store.issue_nonce(addr, chain_id, issued_at,
                                  now_s=now_fn(), ttl_s=cfg.nonce_ttl_s)
        message = build_siwe_message(domain=cfg.siwe_domain, uri=cfg.siwe_uri,
                                     address=addr, chain_id=chain_id,
                                     nonce=nonce, issued_at=issued_at)
        return {"nonce": nonce, "message": message}

    @app.post("/api/auth/verify")
    def auth_verify(body: VerifyBody, response: Response):
        rec = store.consume_nonce(body.nonce, now_s=now_fn())  # 原子單次使用（紅線 4）
        if rec is None:
            raise HTTPException(status_code=401, detail="nonce 不存在、已用過或已過期")
        message = build_siwe_message(domain=cfg.siwe_domain, uri=cfg.siwe_uri,
                                     address=rec.address, chain_id=rec.chain_id,
                                     nonce=body.nonce, issued_at=rec.issued_at)
        try:
            signer = normalize_address(recover_siwe_signer(message, body.signature))
        except Exception:  # noqa: BLE001 — 壞簽名格式一律 401，不洩內部
            raise HTTPException(status_code=401, detail="SIWE 簽名無效") from None
        if signer != rec.address:  # 兩側皆 normalize（工程原則 1：同基準比較）
            raise HTTPException(status_code=401, detail="SIWE 簽名無效")
        sid = store.create_session(signer, now_s=now_fn(), ttl_s=cfg.session_ttl_s)
        response.set_cookie(SESSION_COOKIE, sid, max_age=cfg.session_ttl_s,
                            httponly=True, secure=True, samesite="lax", path="/")
        return {"address": signer, "account_id": derive_account_id(signer)}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            store.delete_session(sid)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(address: str = Depends(_require_session)):
        return {"address": address, "account_id": derive_account_id(address)}

    return app
```

（`keysvc`、`hl` 參數本任務尚未使用——Task 8/9 的端點會用；先入簽名維持 factory 介面穩定。）

- [ ] **Step 5** `uv run pytest tests/test_api_auth.py -q` 全綠 + `uv run ruff check src/spark/publicapi tests`。
- [ ] **Step 6** `git commit -m "feat: FastAPI app factory + SIWE auth endpoints + session cookie"`。

---

### Task 8: onboard agent 生成 + status 端點

**Files:** Modify `src/spark/publicapi/app.py`（`return app` 前加入端點）、Create `tests/test_api_onboard.py`。

註：前端直送 HL 後，**status 就是提交結果的確認機制**（提交回應不經後端，鏈上查詢是唯一真相；斷點續走以此為準）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_api_onboard.py"""
import socket
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.publicapi_helpers import BUILDER, login, make_app

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def test_generate_agent_returns_address(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    agent = r.json()["agent_address"]
    assert agent.startswith("0x") and len(agent) == 42 and agent == agent.lower()
    account_id = "f" + wallet.address.lower()[2:]
    assert keysvc.generated[account_id].lower() == agent
    assert store.get_agent_address(account_id) == agent


def test_generate_agent_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    assert _client(app).post("/api/onboard/agent").status_code == 401


def test_generate_agent_twice_409(tmp_path):
    """防重生：已有 agent 拒絕 rotate（避免作廢既有鏈上授權，沿 M1 語意）。"""
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.post("/api/onboard/agent").status_code == 200
    r = client.post("/api/onboard/agent")
    assert r.status_code == 409
    assert "不重生" in r.json()["detail"]


def test_keysvc_down_502(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    keysvc.fail = ConnectionRefusedError("keysvc down")
    client = _client(app)
    login(client)
    assert client.post("/api/onboard/agent").status_code == 502


def test_desync_self_heals_via_address_op(tmp_path):
    """keysvc 有 key 但 DB 無地址（DB 遺失/回應遺失殘局）→ 唯讀 address op 自癒回填
    （設計定案 12），照常 200，回應帶 recovered=true 供觀測。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    account_id = "f" + wallet.address.lower()[2:]
    keysvc.generated[account_id] = "0x" + "EE" * 20  # 預塞：keystore 有、DB 無
    r = client.post("/api/onboard/agent")
    assert r.status_code == 200
    assert r.json()["recovered"] is True
    assert r.json()["agent_address"] == "0x" + "ee" * 20   # normalize 後回填
    assert store.get_agent_address(account_id) == "0x" + "ee" * 20  # DB 已回填


def test_desync_and_address_also_fails_409(tmp_path):
    """自癒也失敗（address op 打不通）才 409，訊息明確要求人工介入。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    account_id = "f" + wallet.address.lower()[2:]
    keysvc.generated[account_id] = "0x" + "ee" * 20
    keysvc.address_fail = ConnectionRefusedError("keysvc down")
    r = client.post("/api/onboard/agent")
    assert r.status_code == 409
    assert "無法自動復原" in r.json()["detail"]
    assert store.get_agent_address(account_id) is None  # 未寫入半套狀態


def _make_ready(hl, wallet_addr: str, agent: str):
    hl.max_fees[(wallet_addr.lower(), BUILDER.lower())] = 100
    hl.agents[wallet_addr.lower()] = [agent]
    hl.account_values[wallet_addr.lower()] = Decimal("150")


def test_status_progresses_to_ready(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    s0 = client.get("/api/onboard/status").json()
    assert s0["agent_generated"] is False and s0["state"] == "IN_PROGRESS"
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)
    s1 = client.get("/api/onboard/status").json()
    assert s1["agent_generated"] and s1["builder_fee_approved"]
    assert s1["agent_approved"] and s1["funded"]
    assert s1["state"] == "READY"
    assert s1["agent_address"] == agent


def test_status_funding_below_floor_not_ready(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    _make_ready(hl, wallet.address, agent)
    hl.account_values[wallet.address.lower()] = Decimal("99")  # < 100 USDC 門檻
    s = client.get("/api/onboard/status").json()
    assert s["funded"] is False and s["state"] == "IN_PROGRESS"


def test_status_isolated_between_users(tmp_path):
    """紅線 3：account 由 session 衍生——另一個使用者看不到、也影響不了你的進度。"""
    app, *_ = make_app(tmp_path)
    c1, c2 = _client(app), _client(app)
    login(c1)
    login(c2)
    c1.post("/api/onboard/agent")
    assert c2.get("/api/onboard/status").json()["agent_generated"] is False
```

- [ ] **Step 2** 跑到失敗。
- [ ] **Step 3: 實作**（`app.py` 的 `return app` 前加入）

```python
    # ---------- onboarding ----------
    @app.post("/api/onboard/agent")
    def onboard_agent(address: str = Depends(_require_session)):
        account_id = derive_account_id(address)
        store.ensure_onboarding(account_id, address)
        if store.get_agent_address(account_id):
            raise HTTPException(
                status_code=409,
                detail="已有 agent，不重生（避免 rotate 作廢既有鏈上授權）")
        try:
            agent_address = normalize_address(keysvc.generate(account_id))
        except KeysvcError as e:  # 結構化 code 分支——不比對訊息字串（opus 審 M3）
            if e.code == "exists":
                # keystore 有 key、DB 無地址（DB 遺失/回應遺失殘局）：
                # 唯讀 address op 自癒回填（設計定案 12），使用者不卡死。
                try:
                    agent_address = normalize_address(keysvc.address(account_id))
                except Exception as e2:  # noqa: BLE001 — 自癒也失敗才放棄，大聲告警
                    logger.error(
                        "keystore 與 DB 狀態不一致且無法自動復原 account=%s: %s",
                        account_id, e2)
                    raise HTTPException(
                        status_code=409,
                        detail="keystore 與 DB 狀態不一致且無法自動復原，"
                               "請聯絡管理員") from e2
                store.set_agent_address(account_id, agent_address)
                logger.warning("agent 地址自癒回填 account=%s", account_id)
                return {"agent_address": agent_address, "recovered": True}
            logger.error("keysvc generate 失敗 account=%s: %s", account_id, e)
            raise HTTPException(status_code=502, detail="金鑰服務暫時不可用") from e
        except OSError as e:  # socket 連不上等——安全關鍵路徑大聲失敗（工程原則 3）
            logger.error("keysvc 不可達 account=%s: %s", account_id, e)
            raise HTTPException(status_code=502, detail="金鑰服務暫時不可用") from e
        store.set_agent_address(account_id, agent_address)
        return {"agent_address": agent_address}

    def _progress(address: str) -> dict:
        """onboarding 進度：狀態靠鏈上查詢判定（冪等、斷點續走以此為準，沿 M1 精神）。"""
        account_id = derive_account_id(address)
        agent_address = store.get_agent_address(account_id)
        builder_fee_approved = hl.max_builder_fee(address, cfg.builder_address) != 0
        agent_approved = bool(agent_address) and agent_address in hl.agent_addresses(address)
        funded = hl.get_account_value(address) >= cfg.min_user_deposit  # 常數單一來源（M4）
        ready = bool(agent_address) and builder_fee_approved and agent_approved and funded
        return {
            "address": address, "account_id": account_id,
            "agent_address": agent_address,
            "agent_generated": agent_address is not None,
            "builder_fee_approved": builder_fee_approved,
            "agent_approved": agent_approved,
            "funded": funded,
            "state": "READY" if ready else "IN_PROGRESS",
        }

    @app.get("/api/onboard/status")
    def onboard_status(address: str = Depends(_require_session)):
        return _progress(address)  # 純讀；副作用（寫 pending）只在 POST /api/onboard/verify
```

檔頭 import 加：`from spark.keysvc.client import KeysvcError`（結構化 code 分支用；門檻常數走 `cfg.min_user_deposit`/`cfg.min_builder_balance`，不另 import——M4 常數單一來源）。

- [ ] **Step 4** `uv run pytest tests/test_api_onboard.py tests/test_api_auth.py -q` 全綠 + ruff。
- [ ] **Step 5** `git commit -m "feat: onboard agent generation + status endpoints (session-bound, chain-queried state)"`。

---

### Task 9: payload 端點（建 typed data 供瀏覽器簽）⭐

**Files:** Modify `src/spark/publicapi/app.py`、Create `tests/test_api_payload.py`。

**提交路徑註記（設計定案 1）**：前端拿 typed data 簽完後**直接 POST HL `/exchange`**（CORS 已實測全開，見設計定案節），提交結果由 `GET /api/onboard/status` 鏈上查詢確認——**後端不經手簽名，無 submit 端點**。本 task 仍 ⭐：typed data 建錯 = 使用者簽錯東西（agentAddress/builder/maxFeeRate 全部出自伺服器，不收使用者輸入）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_api_payload.py
payload 端點：動態 chainId、agentName、builder 門檻擋。後端無 submit 端點
（前端直送 HL，見計畫設計定案 1）。"""
import socket
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.publicapi_helpers import BUILDER, login, make_app

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


def _setup(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    hl.account_values[BUILDER.lower()] = Decimal("150")  # builder 門檻達標
    return client, wallet, store, hl


def test_payload_agent_requires_generated_agent(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    r = client.post("/api/onboard/payload/approve-agent", json={"chain_id": 42161})
    assert r.status_code == 409


def test_payload_agent_typed_data(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    r = client.post("/api/onboard/payload/approve-agent", json={"chain_id": 42161})
    assert r.status_code == 200
    td = r.json()["typed_data"]
    assert td["domain"]["chainId"] == 42161      # 動態 chainId（research 風險 1）
    assert td["primaryType"] == "HyperliquidTransaction:ApproveAgent"
    assert td["message"]["agentAddress"] == agent
    assert td["message"]["agentName"] == "filet"
    assert td["message"]["hyperliquidChain"] == "Testnet"
    assert td["message"]["signatureChainId"] == "0xa4b1"  # 動態取自前端錢包


def test_payload_builder_fee_typed_data(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    r = client.post("/api/onboard/payload/approve-builder-fee", json={"chain_id": 1})
    assert r.status_code == 200
    td = r.json()["typed_data"]
    assert td["domain"]["chainId"] == 1
    assert td["primaryType"] == "HyperliquidTransaction:ApproveBuilderFee"
    assert td["message"]["builder"] == BUILDER
    assert td["message"]["maxFeeRate"] == "0.1%"


def test_payload_builder_fee_blocked_when_builder_underfunded(tmp_path):
    """spec 錯誤處理：builder 地址 < 100 USDC → builder code 不生效（症狀：成交但
    fee 不累計）——產 payload 時就大聲擋下（沿 M1 BuilderNotEligible 語意）。"""
    client, wallet, store, hl = _setup(tmp_path)
    hl.account_values[BUILDER.lower()] = Decimal("50")
    r = client.post("/api/onboard/payload/approve-builder-fee", json={"chain_id": 1})
    assert r.status_code == 503


def test_payload_bad_chain_id_400(tmp_path):
    client, wallet, store, hl = _setup(tmp_path)
    client.post("/api/onboard/agent")
    r = client.post("/api/onboard/payload/approve-agent", json={"chain_id": 0})
    assert r.status_code == 400


def test_payload_fresh_nonce_each_call(tmp_path):
    """每次呼叫產新 nonce（now_ms）——斷點續走重取 payload 拿到新鮮 nonce，
    舊 typed data 作廢即可（nonce 窗口寬，未簽的舊 payload 無風險）。"""
    client, wallet, store, hl = _setup(tmp_path)
    client.post("/api/onboard/agent")
    n1 = client.post("/api/onboard/payload/approve-agent",
                     json={"chain_id": 1}).json()["typed_data"]["message"]["nonce"]
    import time
    time.sleep(0.002)
    n2 = client.post("/api/onboard/payload/approve-agent",
                     json={"chain_id": 1}).json()["typed_data"]["message"]["nonce"]
    assert n2 >= n1  # 毫秒 timestamp 單調不減


def test_payload_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    for path in ("/api/onboard/payload/approve-agent",
                 "/api/onboard/payload/approve-builder-fee"):
        assert client.post(path, json={"chain_id": 1}).status_code == 401


def test_no_submit_endpoints_exist(tmp_path):
    """紅線 5：後端沒有任何收簽名的 submit 端點（前端直送 HL）。"""
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    for path in ("/api/onboard/submit/approve-agent",
                 "/api/onboard/submit/approve-builder-fee"):
        assert client.post(path, json={"r": "0x1", "s": "0x2", "v": 27}).status_code == 404
```

- [ ] **Step 2** 跑到失敗。
- [ ] **Step 3: 實作**（`app.py` 加入；檔頭 import 區加下列一行）

```python
from spark.publicapi.approvals import build_approve_agent, build_approve_builder_fee
```

Pydantic body model（`VerifyBody` 旁；**注意：全 app 不得出現任何含 r/s/v 欄位的
model——紅線 5，Task 13 有結構性測試**）：

```python
class ChainIdBody(BaseModel):
    chain_id: int
```

端點（`return app` 前）：

```python
    # ---------- 待簽 payload（後端建 typed data，不簽；前端簽完直送 HL /exchange） ----------
    @app.post("/api/onboard/payload/approve-agent")
    def payload_approve_agent(body: ChainIdBody,
                              address: str = Depends(_require_session)):
        account_id = derive_account_id(address)
        agent_address = store.get_agent_address(account_id)
        if not agent_address:
            raise HTTPException(status_code=409,
                                detail="尚未生成 agent，先呼叫 /api/onboard/agent")
        if body.chain_id <= 0:
            raise HTTPException(status_code=400, detail="chain_id 不合法")
        # ⭐ agentAddress/agentName 出自伺服器（keysvc 地址＋設定常數），不收使用者輸入
        typed_data, _action = build_approve_agent(
            agent_address=agent_address, agent_name=cfg.agent_name,
            wallet_chain_id=body.chain_id, is_mainnet=cfg.is_mainnet)
        # action 不落地：前端持有 typed data 簽完直送 HL，提交結果由 status 鏈上查詢確認
        return {"typed_data": typed_data}

    @app.post("/api/onboard/payload/approve-builder-fee")
    def payload_approve_builder_fee(body: ChainIdBody,
                                    address: str = Depends(_require_session)):
        account_id = derive_account_id(address)
        store.ensure_onboarding(account_id, address)
        if body.chain_id <= 0:
            raise HTTPException(status_code=400, detail="chain_id 不合法")
        # builder 啟用門檻（spec 錯誤處理；沿 M1 BuilderNotEligible）：<100 USDC 時
        # builder code 不生效，症狀是「成交但 fee 不累計」——這裡大聲擋下。
        if hl.get_account_value(cfg.builder_address) < cfg.min_builder_balance:
            raise HTTPException(
                status_code=503,
                detail=f"builder 地址餘額低於 {cfg.min_builder_balance} USDC 門檻，"
                       "暫停 onboarding，請聯絡管理員")
        # ⭐ builder/maxFeeRate 出自伺服器設定常數，不收使用者輸入（紅線 6）
        typed_data, _action = build_approve_builder_fee(
            builder=cfg.builder_address, max_fee_rate=cfg.max_fee_rate,
            wallet_chain_id=body.chain_id, is_mainnet=cfg.is_mainnet)
        return {"typed_data": typed_data}
```

- [ ] **Step 4** `uv run pytest tests/test_api_payload.py tests/test_api_onboard.py tests/test_api_auth.py -q` 全綠 + ruff。
- [ ] **Step 5** `git commit -m "feat: approval payload endpoints — typed data for browser signing, direct-to-HL submission"`。

---

### Task 10: verify 端點 + pending 佇列 + admin 唯讀

**Files:** Create `src/spark/publicapi/pending.py`、Modify `src/spark/publicapi/app.py`、Create `tests/test_api_verify_admin.py`。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_api_verify_admin.py
verify（READY → 寫 pending）＋ pending.json 讀寫 ＋ admin 白名單。"""
import socket
from decimal import Decimal

import pytest
from eth_account import Account
from fastapi.testclient import TestClient

from spark.publicapi.pending import load_pending, remove_pending_entry, write_pending_entry
from tests.publicapi_helpers import BUILDER, login, make_app, make_cfg

_REAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _client(app):
    return TestClient(app, base_url="https://testserver")


# --- pending.py 單元測試 ---

def _entry(acct="f" + "ab" * 20):
    return dict(account_id=acct, user_address="0x" + "ab" * 20,
                builder_address=BUILDER, network="testnet",
                agent_address="0x" + "cd" * 20)


def test_write_pending_idempotent(tmp_path):
    p = tmp_path / "pending.json"
    write_pending_entry(p, **_entry())
    write_pending_entry(p, **_entry())  # 同 account 再寫 → no-op
    assert len(load_pending(p)) == 1


def test_write_pending_validates(tmp_path):
    p = tmp_path / "pending.json"
    with pytest.raises(ValueError):
        write_pending_entry(p, **{**_entry(), "account_id": "../evil"})
    with pytest.raises(ValueError):
        write_pending_entry(p, **{**_entry(), "network": "devnet"})
    assert load_pending(p) == []


def test_remove_pending(tmp_path):
    p = tmp_path / "pending.json"
    write_pending_entry(p, **_entry())
    remove_pending_entry(p, _entry()["account_id"])
    assert load_pending(p) == []


# --- verify 端點 ---

def _make_ready(client, hl, wallet):
    agent = client.post("/api/onboard/agent").json()["agent_address"]
    hl.max_fees[(wallet.address.lower(), BUILDER.lower())] = 100
    hl.agents[wallet.address.lower()] = [agent]
    hl.account_values[wallet.address.lower()] = Decimal("150")
    return agent


def test_verify_not_ready_no_pending(tmp_path):
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    login(client)
    r = client.post("/api/onboard/verify")
    assert r.status_code == 200
    assert r.json()["state"] == "IN_PROGRESS"  # 斷點續走：回哪些檢查沒過
    assert load_pending(cfg.pending_path) == []


def test_verify_ready_writes_pending_bound_to_session(tmp_path):
    """⭐ 紅線 6：user_address 綁 session、builder_address 是伺服器常數。"""
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    client = _client(app)
    wallet = login(client)
    agent = _make_ready(client, hl, wallet)
    r = client.post("/api/onboard/verify")
    assert r.status_code == 200 and r.json()["state"] == "READY"
    entries = load_pending(cfg.pending_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["account_id"] == "f" + wallet.address.lower()[2:]
    assert e["user_address"] == wallet.address.lower()   # 出自 session，非請求輸入
    assert e["builder_address"] == BUILDER               # 伺服器常數
    assert e["network"] == "testnet"
    assert e["agent_address"] == agent
    # 重呼冪等：仍只有一條
    client.post("/api/onboard/verify")
    assert len(load_pending(cfg.pending_path)) == 1


def test_admin_pending_403_for_non_admin(tmp_path):
    app, *_ = make_app(tmp_path)
    client = _client(app)
    login(client)
    assert client.get("/api/admin/pending").status_code == 403


def test_admin_pending_ok_for_whitelisted(tmp_path):
    admin_wallet = Account.create()
    cfg = make_cfg(tmp_path,
                   admin_addresses=frozenset({admin_wallet.address.lower()}))
    app, cfg, store, keysvc, hl = make_app(tmp_path, cfg=cfg)
    client = _client(app)
    login(client, wallet=admin_wallet)
    r = client.get("/api/admin/pending")
    assert r.status_code == 200
    assert r.json() == {"pending": []}


def test_admin_pending_requires_session(tmp_path):
    app, *_ = make_app(tmp_path)
    assert _client(app).get("/api/admin/pending").status_code == 401
```

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作 `src/spark/publicapi/pending.py`**

```python
"""src/spark/publicapi/pending.py
Pending follower 佇列（pending.json，filet-api 擁有）——與引擎 followers.json 刻意
分檔：web 層只寫 pending；followers.json 只由人工 activate CLI（管理端）寫。
權限拓撲：filet-api 對引擎 manifest 本就不該有寫權。條目的 user_address 綁 SIWE
session、builder_address 是伺服器常數（app 層保證；CLI 再核對一次）。"""
import json
import os
from pathlib import Path

from spark.filet.followers import validate_account_id

_NETWORKS = {"testnet", "mainnet"}


def load_pending(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return json.loads(p.read_text()).get("pending", [])


def _atomic_write(p: Path, entries: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pending": entries}, indent=2))
    os.replace(tmp, p)  # 原子換檔，不留半寫


def write_pending_entry(path: str | Path, *, account_id: str, user_address: str,
                        builder_address: str, network: str, agent_address: str,
                        label: str = "") -> None:
    """冪等：同 account_id 已在佇列即 no-op。寫入前驗證（縱深防禦）。"""
    validate_account_id(account_id)
    if network not in _NETWORKS:
        raise ValueError(f"network 須為 {_NETWORKS}: {network!r}")
    p = Path(path)
    entries = load_pending(p)
    if any(e.get("account_id") == account_id for e in entries):
        return
    entries.append({"account_id": account_id, "user_address": user_address,
                    "builder_address": builder_address, "network": network,
                    "agent_address": agent_address, "label": label})
    _atomic_write(p, entries)


def remove_pending_entry(path: str | Path, account_id: str) -> None:
    p = Path(path)
    _atomic_write(p, [e for e in load_pending(p)
                      if e.get("account_id") != account_id])
```

- [ ] **Step 4: `app.py` 加端點**（檔頭 import 加 `from spark.publicapi.pending import load_pending, write_pending_entry`）

```python
    @app.post("/api/onboard/verify")
    def onboard_verify(address: str = Depends(_require_session)):
        """檢查全過 → 寫 pending 條目（等管理端人工 CLI 核准；spec：activate 不做成
        API 端點）。未全過 → 回進度供斷點續走（冪等，可重跑）。"""
        p = _progress(address)
        if p["state"] == "READY":
            # ⭐ user_address 出自 session、builder_address 出自伺服器設定（紅線 6）
            write_pending_entry(cfg.pending_path, account_id=p["account_id"],
                                user_address=address,
                                builder_address=cfg.builder_address,
                                network=cfg.network,
                                agent_address=p["agent_address"])
        return p

    @app.get("/api/admin/pending")
    def admin_pending(address: str = Depends(_require_session)):
        """管理端唯讀：檢視 pending 清單（逐筆核對 builder_address 用）。啟用走人工
        CLI scripts/filet_activate.py，web 層無任何 systemd/寫 manifest 權。"""
        if address not in cfg.admin_addresses:  # 兩側皆 normalize 過
            raise HTTPException(status_code=403, detail="非管理員")
        return {"pending": load_pending(cfg.pending_path)}
```

- [ ] **Step 5** `uv run pytest tests/test_api_verify_admin.py -q` 全綠；`uv run pytest -q` 全套綠 + ruff。
- [ ] **Step 6** `git commit -m "feat: verify endpoint + pending queue + admin read-only list (session-bound user, server-constant builder)"`。

---

### Task 11: activate CLI（人工核可）⭐

**Files:** Create `scripts/filet_activate.py`、`tests/test_filet_activate.py`。
先讀 `src/spark/filet/followers.py`（manifest 格式與 fail-fast 載入）、`deploy/filet-follower@.service`（unit 名稱格式 `filet-follower@<account_id>`）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_filet_activate.py
activate：pending → followers.json（人工 CLI；builder 結構性核對）。純檔案操作，離線。"""
import json

import pytest

from scripts.filet_activate import activate
from spark.filet.followers import load_followers
from spark.publicapi.pending import load_pending, write_pending_entry

_BUILDER = "0x" + "b1" * 20
_USER = "0x" + "ab" * 20
_ACCT = "f" + "ab" * 20


def _setup(tmp_path, builder=_BUILDER):
    pending = tmp_path / "pending.json"
    manifest = tmp_path / "followers.json"
    write_pending_entry(pending, account_id=_ACCT, user_address=_USER,
                        builder_address=builder, network="testnet",
                        agent_address="0x" + "cd" * 20, label="alice")
    return pending, manifest


def test_activate_moves_entry_to_manifest(tmp_path):
    pending, manifest = _setup(tmp_path)
    msg = activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False)
    refs = load_followers(manifest)  # fail-fast 載入 = 引擎視角驗證
    assert len(refs) == 1
    assert refs[0].account_id == _ACCT
    assert refs[0].user_address == _USER
    assert refs[0].builder_address == _BUILDER
    assert refs[0].network == "testnet"
    assert refs[0].label == "alice"
    assert load_pending(pending) == []          # 已從佇列移除
    assert f"filet-follower@{_ACCT}" in msg     # 印出啟動指令（預設不執行）


def test_activate_rejects_builder_mismatch(tmp_path):
    """⭐ 紅線 6：pending 條目 builder != 部署常數 → 條目可疑，拒絕啟用。"""
    pending, manifest = _setup(tmp_path, builder="0x" + "ee" * 20)
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False)
    assert not manifest.exists()                # manifest 未被碰
    assert len(load_pending(pending)) == 1      # 條目留在佇列供調查


def test_activate_rejects_duplicate_in_manifest(tmp_path):
    pending, manifest = _setup(tmp_path)
    manifest.write_text(json.dumps({"followers": [{
        "account_id": _ACCT, "user_address": _USER,
        "builder_address": _BUILDER, "network": "testnet"}]}))
    with pytest.raises(SystemExit):
        activate(_ACCT, str(pending), str(manifest), _BUILDER, start=False)


def test_activate_unknown_account(tmp_path):
    pending, manifest = _setup(tmp_path)
    with pytest.raises(SystemExit):
        activate("f" + "99" * 20, str(pending), str(manifest), _BUILDER, start=False)


def test_activate_case_insensitive_builder_check(tmp_path):
    """比對前同 normalize 基準（工程原則 1）：大小寫不同不該誤判。"""
    pending, manifest = _setup(tmp_path)
    activate(_ACCT, str(pending), str(manifest), _BUILDER.upper().replace("0X", "0x"),
             start=False)
    assert len(load_followers(manifest)) == 1
```

- [ ] **Step 2** 跑到失敗（ImportError）。
- [ ] **Step 3: 實作 `scripts/filet_activate.py`**

```python
"""scripts/filet_activate.py
管理端人工核可 CLI（spec：activate 不做成 API 端點——對外 web 層若能拉 systemd 需提權，
被打穿即取得 unit 控制；危險 OS 動作收斂在人工 CLI）。
流程：讀 pending 條目 → 結構性核對 builder_address == FILET_BUILDER_ADDR（杜絕 web 層
被打穿後注入指向攻擊者的 builder 條目）→ 寫入 followers.json（拒絕重複）→ 以
load_followers fail-fast 重讀驗證 → 自 pending 移除 → 印出（或 --start 執行）
systemctl 啟動指令。
用法: FILET_BUILDER_ADDR=0x... uv run python -m scripts.filet_activate <account_id> \\
      [--pending var/filet/pending.json] [--manifest var/filet/followers.json] [--start]
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

from spark.filet.followers import load_followers
from spark.publicapi.config import normalize_address
from spark.publicapi.pending import load_pending, remove_pending_entry


def activate(account_id: str, pending_path: str, manifest_path: str,
             expected_builder: str, *, start: bool) -> str:
    matches = [e for e in load_pending(pending_path)
               if e.get("account_id") == account_id]
    if not matches:
        raise SystemExit(f"pending 中找不到 account_id={account_id}")
    entry = matches[0]
    # ⭐ 結構性核對（紅線 6）：builder 必須等於部署設定常數；比對前同 normalize 基準。
    if normalize_address(entry["builder_address"]) != normalize_address(expected_builder):
        raise SystemExit(
            f"builder_address 不符！pending={entry['builder_address']} "
            f"期望={expected_builder} —— 條目可疑，拒絕啟用（條目保留供調查）")
    manifest = Path(manifest_path)
    data = json.loads(manifest.read_text()) if manifest.exists() else {"followers": []}
    if any(f.get("account_id") == account_id for f in data["followers"]):
        raise SystemExit(f"{account_id} 已在 followers.json，拒絕重複啟用")
    data["followers"].append({
        "account_id": account_id,
        "user_address": normalize_address(entry["user_address"]),
        "builder_address": normalize_address(entry["builder_address"]),
        "network": entry["network"],
        "label": entry.get("label", ""),
    })
    manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, manifest)
    load_followers(manifest)  # fail-fast 重讀：寫壞立刻大聲炸（引擎同一載入路徑）
    remove_pending_entry(pending_path, account_id)
    cmd = f"systemctl start filet-follower@{account_id}"
    if start:
        subprocess.run(["systemctl", "start", f"filet-follower@{account_id}"],
                       check=True)
        return f"已寫入 manifest 並啟動: {cmd}"
    return f"已寫入 manifest。請人工啟動: {cmd}"


def main() -> None:
    ap = argparse.ArgumentParser(description="人工核可 pending follower 並寫入 manifest")
    ap.add_argument("account_id")
    ap.add_argument("--pending", default="var/filet/pending.json")
    ap.add_argument("--manifest", default="var/filet/followers.json")
    ap.add_argument("--start", action="store_true",
                    help="寫入後直接 systemctl start（預設只印指令）")
    args = ap.parse_args()
    builder = os.environ.get("FILET_BUILDER_ADDR")
    if not builder:
        print(__doc__)
        print("缺少環境變數 FILET_BUILDER_ADDR（核對 pending 條目的 builder 用）")
        raise SystemExit(2)
    print(activate(args.account_id, args.pending, args.manifest, builder,
                   start=args.start))


if __name__ == "__main__":
    main()
```

註：manifest 條目不含 agent_address——`FollowerRef` 無此欄位（引擎以 account_id 從
keystore 讀 agent key）；pending 條目裡的 agent_address 是管理端核對用的資訊欄。

- [ ] **Step 4** `uv run pytest tests/test_filet_activate.py tests/test_scripts_import.py -q` 全綠（scripts import 測試確認新 CLI import 期零副作用）+ ruff。
- [ ] **Step 5** `git commit -m "feat: filet_activate CLI — human-approved pending-to-manifest promotion with builder pin check"`。

---

### Task 12: uvicorn 入口 + systemd unit

**Files:** Create `scripts/run_api.py`、`deploy/filet-api.service`。無單元測試（入口/部署檔）；驗收＝read-back + 無 env 執行印用法 + `tests/test_scripts_import.py` 綠。

- [ ] **Step 1: `scripts/run_api.py`**

```python
"""Public API 入口。
用法: FILET_API_NETWORK=testnet FILET_BUILDER_ADDR=0x.. FILET_SIWE_DOMAIN=filet.example \\
      FILET_SIWE_URI=https://filet.example FILET_API_DB=var/filet/api.db \\
      FILET_KEYSVC_SOCK=/run/filet/keysvc.sock FILET_PENDING_PATH=var/filet/pending.json \\
      [FILET_ADMIN_ADDRESSES=0x..,0x..] [FILET_API_PORT=8700] \\
      uv run python -m scripts.run_api
（生產由 systemd 拉起、跑在 filet-api user；只綁 127.0.0.1，對外經反向代理 TLS。）"""
import os


def main() -> None:
    # 依賴延後到 main 內 import（import 階段零副作用，沿 run_keysvc 慣例）
    from spark.publicapi.config import ApiConfig
    try:
        cfg = ApiConfig.from_env()
    except ValueError as e:
        print(__doc__)
        print(f"設定錯誤: {e}")
        raise SystemExit(2) from e
    import uvicorn

    from spark.keysvc.client import KeysvcClient
    from spark.publicapi.app import create_app
    from spark.publicapi.hl import HLGateway
    from spark.publicapi.store import ApiStore
    app = create_app(cfg, ApiStore(cfg.db_path), KeysvcClient(cfg.keysvc_sock),
                     HLGateway(cfg.api_url))
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("FILET_API_PORT", "8700")))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: `deploy/filet-api.service`**

```ini
[Unit]
Description=Filet public API (SIWE onboarding backend, localhost only, behind reverse proxy)
After=network.target filet-keysvc.service
Wants=filet-keysvc.service

[Service]
Type=simple
User=filet-api
Group=filet-api
WorkingDirectory=/opt/filet/spark
Environment=FILET_API_NETWORK=mainnet
Environment=FILET_BUILDER_ADDR=REPLACE_WITH_BUILDER_ADDRESS
Environment=FILET_SIWE_DOMAIN=REPLACE_WITH_DASHBOARD_DOMAIN
Environment=FILET_SIWE_URI=REPLACE_WITH_DASHBOARD_URI
Environment=FILET_API_DB=/var/lib/filet-api/api.db
Environment=FILET_KEYSVC_SOCK=/run/filet/keysvc.sock
Environment=FILET_PENDING_PATH=/var/lib/filet-api/pending.json
Environment=FILET_ADMIN_ADDRESSES=REPLACE_WITH_ADMIN_ADDRESSES
Environment=FILET_API_PORT=8700
StateDirectory=filet-api
ExecStart=/opt/filet/spark/.venv/bin/python -m scripts.run_api
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/filet-api /run/filet
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

（`REPLACE_WITH_*` 是部署時填實際值的**明確佔位**，非程式 placeholder——沿
`deploy/filet-keysvc.service` 慣例；filet-api 對 `/run/filet` 只需 socket 連線權，
socket 檔權限與 group 歸屬由 keysvc 部署設定，本 unit 不重複宣告。）

- [ ] **Step 3** 無 env 跑 `uv run python -m scripts.run_api` → 印用法、exit 2（不觸網、不開 port）。`uv run pytest tests/test_scripts_import.py -q` 綠（import 期零副作用）。
- [ ] **Step 4** `git commit -m "feat: public API entrypoint + systemd unit (filet-api, localhost only)"`。

---

### Task 13: 離線端到端 + 非託管不變量 ⭐（加 opus 第二意見）

**Files:** Create `tests/test_publicapi_integration.py`。
整條真跑：真 SIWE 簽名 → **真 key-service**（unix socket 執行緒，真 keystore 落檔）→
取 payload → 模擬瀏覽器真簽 typed data（證明可簽；簽名**不送後端**）→ 前端直送 HL 以
**FakeHL 狀態翻轉**模擬（授權上鏈）→ verify → pending → activate CLI → 引擎視角讀
manifest 與 keystore。唯二 fake：HL 鏈上狀態、systemd。非託管不變量掃描含**結構性證明
後端不收簽名**（API 表面無 r/s/v 欄位）。

- [ ] **Step 1: 失敗測試**

```python
"""tests/test_publicapi_integration.py
離線端到端：SIWE → keysvc（真 socket + 真 keystore）→ payload → 瀏覽器簽名模擬
（簽名不送後端；上鏈以 FakeHL 狀態翻轉模擬）→ verify → pending → activate。
非託管不變量：主鑰/agent 私鑰/EIP-712 簽名不出現在任何 HTTP 回應、DB、
pending/manifest 檔；API 表面結構上無收簽名欄位。"""
import inspect
import json
import socket
import threading
import uuid
from decimal import Decimal
from pathlib import Path

from eth_account.messages import encode_typed_data
from fastapi.testclient import TestClient

import pytest

from scripts.filet_activate import activate
from spark.filet.followers import load_followers
from spark.keystore.envfile import EnvFileKeyStore
from spark.keysvc.client import KeysvcClient
from spark.keysvc.server import serve_forever
from spark.publicapi.app import create_app
from spark.publicapi.pending import load_pending
from spark.publicapi.store import ApiStore
from tests.publicapi_helpers import BUILDER, FakeHL, login, make_cfg

_REAL_SOCKET = socket.socket  # import 期捕捉（keysvc 慣例）


@pytest.fixture(autouse=True)
def _allow_local_sockets(monkeypatch):
    monkeypatch.setattr(socket, "socket", _REAL_SOCKET)


def _connect_when_ready(sock_path):
    """沿 tests/test_keysvc_client.py：重試 connect 避開 bind/listen 窗口 race。"""
    import time
    for _ in range(100):
        c = _REAL_SOCKET(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.connect(str(sock_path))
            c.close()
            return
        except (FileNotFoundError, ConnectionRefusedError):
            c.close()
            time.sleep(0.02)
    raise RuntimeError("keysvc 測試: server 未就緒")


def test_full_onboarding_flow_offline(tmp_path):
    # --- 起真 key-service（授權器全放行；peercred 已在 keysvc 測試驗）---
    sock_path = Path(f"/tmp/spark-publicapi-e2e-{uuid.uuid4().hex[:8]}.sock")
    keys_dir = tmp_path / "keys"
    ks = EnvFileKeyStore(keys_dir)
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: True, stop), daemon=True)
    t.start()
    _connect_when_ready(sock_path)
    try:
        cfg = make_cfg(tmp_path, keysvc_sock=str(sock_path))
        store = ApiStore(cfg.db_path)
        hl = FakeHL()
        app = create_app(cfg, store, KeysvcClient(str(sock_path)), hl)
        client = TestClient(app, base_url="https://testserver")
        captured: list[str] = []          # 所有 HTTP 回應原文（非託管掃描用）

        def post(path, **kw):
            r = client.post(path, **kw)
            captured.append(r.text)
            return r

        # 1. SIWE 登入（真簽名）
        wallet = login(client)
        account_id = "f" + wallet.address.lower()[2:]
        # 2. 生成 agent（經真 keysvc；私鑰落 keystore、API 只見地址）
        r = post("/api/onboard/agent")
        assert r.status_code == 200
        agent_addr = r.json()["agent_address"]
        assert ks.get_agent_signer(account_id).address.lower() == agent_addr
        # 3. 產兩個 payload → 模擬瀏覽器真簽 typed data（證明可簽）。
        #    簽名「不」送後端——前端直送 HL /exchange（設計定案 1，CORS 已實測）；
        #    本測試以步驟 4 的 FakeHL 狀態翻轉模擬「授權已上鏈」。
        hl.account_values[BUILDER.lower()] = Decimal("150")
        browser_sigs = []
        for kind in ("approve-agent", "approve-builder-fee"):
            r = post(f"/api/onboard/payload/{kind}", json={"chain_id": 42161})
            assert r.status_code == 200
            td = r.json()["typed_data"]
            sm = wallet.sign_message(encode_typed_data(full_message=td))
            browser_sigs.append(hex(sm.r).removeprefix("0x"))
        # 4. 模擬鏈上生效（前端直送的結果）→ verify → pending
        hl.max_fees[(wallet.address.lower(), BUILDER.lower())] = 100
        hl.agents[wallet.address.lower()] = [agent_addr]
        hl.account_values[wallet.address.lower()] = Decimal("150")
        r = post("/api/onboard/verify")
        assert r.json()["state"] == "READY"
        assert len(load_pending(cfg.pending_path)) == 1
        # 5. 人工 activate → 引擎視角：manifest 讀得到、keystore 有 key
        manifest = tmp_path / "followers.json"
        activate(account_id, cfg.pending_path, str(manifest), BUILDER, start=False)
        refs = load_followers(manifest)
        assert refs[0].account_id == account_id
        assert refs[0].user_address == wallet.address.lower()
        assert ks.get_agent_signer(account_id).address.lower() == agent_addr
        # --- 非託管不變量掃描 ---
        master_pk = wallet.key.hex().removeprefix("0x")
        agent_pk = (keys_dir / account_id / "agent.key").read_text().strip() \
            .removeprefix("0x")
        blobs = {
            "http 回應": "\n".join(captured),
            "sqlite DB": Path(cfg.db_path).read_bytes().hex()
                          + Path(cfg.db_path).read_bytes().decode("latin1"),
            "pending/manifest": json.dumps(load_pending(cfg.pending_path))
                                 + manifest.read_text(),
        }
        for name, blob in blobs.items():
            assert master_pk not in blob, f"主鑰出現在 {name}"
            assert agent_pk not in blob, f"agent 私鑰出現在 {name}"
            # EIP-712 授權簽名從未進後端：任何簽名值不得出現在伺服器側任何地方
            for sig_r in browser_sigs:
                assert sig_r not in blob, f"授權簽名出現在 {name}"
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)


def test_desync_self_heal_contract_with_real_keysvc(tmp_path):
    """opus 審 I2 要求的真 keysvc desync 契約測試：API DB 遺失後重呼 /agent，
    經 keysvc 唯讀 address op 自癒回填，地址與 keystore 落檔一致（設計定案 12）。"""
    sock_path = Path(f"/tmp/spark-publicapi-heal-{uuid.uuid4().hex[:8]}.sock")
    keys_dir = tmp_path / "keys"
    ks = EnvFileKeyStore(keys_dir)
    stop = threading.Event()
    t = threading.Thread(target=serve_forever,
                         args=(str(sock_path), ks, lambda s: True, stop), daemon=True)
    t.start()
    _connect_when_ready(sock_path)
    try:
        cfg = make_cfg(tmp_path, keysvc_sock=str(sock_path))
        store = ApiStore(cfg.db_path)
        app = create_app(cfg, store, KeysvcClient(str(sock_path)), FakeHL())
        client = TestClient(app, base_url="https://testserver")
        wallet = login(client)
        account_id = "f" + wallet.address.lower()[2:]
        first = client.post("/api/onboard/agent").json()["agent_address"]
        # 模擬 API DB 遺失（onboarding 表清空；keystore 的 key 檔仍在）
        with store._lock, store._db:  # noqa: SLF001 — 測試直接清表模擬災難情境
            store._db.execute("DELETE FROM onboarding")
        r = client.post("/api/onboard/agent")
        assert r.status_code == 200
        assert r.json().get("recovered") is True
        assert r.json()["agent_address"] == first          # 同一把 key、同一地址
        assert ks.get_agent_signer(account_id).address.lower() == first  # 與 keystore 一致
    finally:
        stop.set()
        t.join(timeout=2)
        sock_path.unlink(missing_ok=True)


def test_api_surface_has_no_rsv_signature_fields(tmp_path):
    """紅線 5 的結構性證明：整個 API 的 request model 沒有任何 r/s/v 欄位——
    後端經手 EIP-712 授權簽名在型別層就不可能。唯一的簽名欄位是 SIWE 登入的
    `signature`（EIP-191 身分驗證，性質不同、刻意保留）。"""
    from pydantic import BaseModel

    import spark.publicapi.app as app_mod
    models = [obj for _, obj in inspect.getmembers(app_mod)
              if inspect.isclass(obj) and issubclass(obj, BaseModel)
              and obj is not BaseModel]
    assert models, "app 模組應至少有一個 request model（防呆：抓錯模組會誤判通過）"
    for model in models:
        assert not {"r", "s", "v"} & set(model.model_fields), (
            f"{model.__name__} 含 r/s/v 欄位——後端不得收 EIP-712 簽名（紅線 5）")


def test_cross_user_cannot_touch_others_onboarding(tmp_path):
    """紅線 3 整條驗：B 登入後的所有 onboarding 動作只落在 B 自己的 account。"""
    from tests.publicapi_helpers import make_app
    app, cfg, store, keysvc, hl = make_app(tmp_path)
    ca = TestClient(app, base_url="https://testserver")
    cb = TestClient(app, base_url="https://testserver")
    wa = login(ca)
    login(cb)
    agent_a = ca.post("/api/onboard/agent").json()["agent_address"]
    # B 生成的是 B 自己的 agent，A 的不受影響
    agent_b = cb.post("/api/onboard/agent").json()["agent_address"]
    assert agent_a != agent_b
    acct_a = "f" + wa.address.lower()[2:]
    assert store.get_agent_address(acct_a) == agent_a
```

- [ ] **Step 2** 跑到失敗（若前面任務都完成則可能直接綠——那也 OK，此為整合守門）。
- [ ] **Step 3** 若有紅則修對應層；`uv run pytest -q` 全套綠 + `uv run ruff check src tests scripts`。
- [ ] **Step 4** `git commit -m "test: public API offline end-to-end + non-custodial invariants (no key or signature ever server-side, session isolation)"`。
- [ ] **Step 5** ⭐ **opus 對抗性第二意見**：fresh-context opus 只拿「全域紅線 1-6 + spec 不變量 1-3 + 本測試檔與 `src/spark/publicapi/`」，獨立判斷：有沒有任何路徑讓（a）主鑰或 agent 私鑰進入後端欄位/log/回應；（b）非 session 本人觸發他人 onboarding；（c）後端出現任何收 EIP-712 簽名的表面（r/s/v 欄位或等價物）或 nonce 重放。發現即回 Task 修正；結論記錄於本計畫執行狀態節。

---

## 收尾（全計畫完成後）

1. 指揮官親跑 `uv run pytest -q`（全套：基線 570 + 本計畫新增全綠）+ `uv run ruff check src tests scripts`。
2. 更新本計畫頂部加「執行狀態」節（任務→commit 對照、opus 第二意見結論），commit。
3. **不 push、不動 main**；交付後等下一個計畫（Dashboard 前端），部署驗收（reverse proxy TLS、filet-api 讀不到 keystore 的實機權限測試）留部署計畫。

## Spec 覆蓋對照（spec 決策 → task）

| Spec 需求/決策 | Task |
|---|---|
| SIWE nonce 發放（綁地址+過期）`POST /auth/nonce` → 本計畫 `GET /api/auth/nonce` | 4, 7 |
| SIWE 驗簽建 session（httpOnly/Secure/SameSite=Lax cookie）`POST /auth/verify` | 3, 7 |
| nonce 單次使用（防有效期內重放）＋訊息綁 domain/URI | 3, 4, 7 |
| `POST /auth/logout`、`GET /me`（address + onboarding 狀態） | 7（/me 回 address+account_id；完整進度在 /api/onboard/status，Task 8） |
| 生成 agent 經 key-service socket、account_id = "f"+完整 40hex、已存在拒絕重生 | 2（衍生）、8（端點；keysvc O_EXCL 語意已在 key-service 計畫） |
| keysvc「無其他操作」→ 本計畫加唯讀 `address` op（**spec deviation**，設計定案 12；desync 自癒） | 1, 8, 13 |
| 產兩筆待簽 EIP-712 payload（ApproveAgent / ApproveBuilderFee maxRate 0.1%） | 5（builder）、9（端點；spec 的單一 GET /approvals 拆成兩個 POST 以收 wallet chainId——research 風險 1） |
| 簽好的授權**前端直送 HL /exchange**、後端不經手已簽交易（spec 步驟 5 原文；CORS 實測全開，設計定案 1） | 6（gateway 無寫入面）、9（註記）、13（結構性測試）＋前端計畫（實際直送、v 正規化） |
| verify：maxBuilderFee != 0 + agent 授權生效 + 入金 ≥ 100 USDC → READY（斷點續走、冪等） | 8（status 純讀）、10（verify 寫 pending） |
| builder 門檻 < 100 USDC 在產 payload 時明確擋下（BuilderNotEligible 語意） | 9 |
| pending manifest：user_address 綁 session、builder_address 伺服器常數 | 10 |
| `GET /admin/pending` 唯讀 + 管理員地址白名單 | 10 |
| activate 不做成 API 端點：人工 CLI `scripts/filet_activate.py` 寫 manifest + 拉 unit | 11 |
| Session store：SQLite 單檔（開放項 1 拍板） | 4 |
| 帳戶級授權檢查（session == account）→ 升級為結構性（account 由 session 衍生、無輸入） | 7-10 全部端點 |
| 錯誤處理：keysvc 不可達 500 級大聲告警、已存在明確拒絕（含 desync 自癒）、驗簽失敗 401 不洩內部 | 7, 8, 9 |
| systemd：`filet-api.service`（filet-api user、硬化、對 keysvc socket 連線） | 12 |
| 測試：SIWE（過期/錯簽/重放）、payload 對照 wire 格式、cookie 屬性、跨帳戶擋下、mock HL 離線 | 1, 3-10, 13 |
| **未覆蓋（刻意）**：`GET /perf/{account}` 唯讀績效（dashboard 讀取面，指揮官範圍排除→前端/後續計畫）；dashboard label XSS 轉義（前端計畫）；反代 TLS 與實機權限驗收（部署計畫）；律師背書（M0 gate，非工程） | — |

## 不在本計畫（各自後續，見 spec 拆解）

- **Dashboard 前端**（Next.js，v1 token，wizard 4 階段；label 顯示轉義防 stored XSS）。
- **前端的提交職責（設計定案 1 移交，前端計畫要接）**：拿 typed data 用錢包 `eth_signTypedData_v4` 簽 → **v 正規化**（0/1→27/28，viem/wagmi 通常已處理）→ 組 `{action, nonce, signature, vaultAddress: null, expiresAfter: null}` **簽後立即直送 HL /exchange**（降低 nonce 窗口內簽名重放暴露；CORS 已實測全開）→ 以 `GET /api/onboard/status` 確認上鏈。research 的 recover 預驗職責亦在前端（錢包本就只能以連線帳號簽，風險低）。
- **部署**（反向代理 TLS、實機權限驗收：filet-api/filet-dashboard 讀不到 agent.key、SO_PEERCRED 實機生效、systemd 全套拉起）。
- **testnet 整合實測**（spec 測試節：真 MetaMask 簽 chainId 最終確認〔research 建議〕、全流程 ≥2 錢包——屬部署後人工驗收，非離線測試套件）。
- M3（Stripe、多 leader）；engine 程式碼修改（followers.py/keystore 只消費不改）。keysvc 唯一的例外是 Task 1 的唯讀 `address` op＋error code（spec deviation，設計定案 12），不動金鑰生成/寫入路徑。

## 運維註記（M5＋opus 觀察；記錄，不擋開工）

- **nonce/session 定期回收**：目前過期列僅邏輯失效（查詢帶 expiry 條件），不會物理刪除——後續小 task 加「consume 即刪＋過期 reaper」。
- **CORS／同源**：前端直送 HL 依賴瀏覽器可跨域呼叫 HL（已實測 ACAO:*）；dashboard ↔ API 的同源由部署反代保證——**移交部署計畫清單點名**。
- **activate 前重查鏈上狀態**：admin 核准當下可 re-verify（授權仍生效、入金仍達標）再拉 unit；目前人工閘可接受，記為 future。
