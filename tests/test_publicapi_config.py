"""tests/test_publicapi_config.py"""
from pathlib import Path

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
        "FILET_EXCHANGE_DIR": "/tmp/filet-exchange",
        "FILET_STATE_BASE": "/opt/filet/state",
        "FILET_LEADERS_PATH": "/opt/filet/spark/var/filet/leaders.json",
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


# ── ⭐ 交換目錄：半邊漏設必須大聲失敗（opus 審查 I2）────────────────────

def test_exchange_dir_is_required_and_has_no_silent_default():
    """⭐ 漏設 FILET_EXCHANGE_DIR → **拒絕啟動**，不得靜默退回某個預設值。

    這個變數的兩端是兩個 systemd unit（filet-api 寫、filet-follower@ 讀）。舊設計
    給了隱含 fallback，於是漏設的症狀是靜默的：API 寫在 A、引擎讀 B，客戶按了換
    leader、API 回 200 說「下一個 cycle 生效」，而它永遠不會生效——兩邊 log 都正常。
    「起不來」比「起來了但功能靜默失效」早好幾天被發現。
    """
    with pytest.raises(ValueError, match="FILET_EXCHANGE_DIR"):
        ApiConfig.from_env(_env(FILET_EXCHANGE_DIR=None))


def test_state_base_is_required_and_has_no_silent_default():
    """⭐⭐ 漏設 FILET_STATE_BASE → **拒絕啟動**（與 FILET_EXCHANGE_DIR 同一處理）。

    這個變數與引擎 unit 的 `FILET_STATE_DIR=/opt/filet/state/%i` 是同一條路徑的
    **兩份獨立推導**，沒有共同的仲裁者。舊版有隱含預設 `/opt/filet/state`，於是
    漏設／設錯的症狀是靜默的：API 去讀一個引擎沒在寫的目錄，每個 follower 的狀態根
    都 `absent`，而面板當時把 absent 讀成「kill switch 未觸發」——在引擎已經熔斷、
    部位已被平掉的當下報告一切正常。「起不來」刻意優先於「起來了但面板謊報健康」。
    """
    with pytest.raises(ValueError, match="FILET_STATE_BASE"):
        ApiConfig.from_env(_env(FILET_STATE_BASE=None))


def test_state_base_has_no_class_level_default_either():
    """⭐ 連 dataclass 層都沒有預設值（沿 exchange_dir 的同一個結構性決定）：
    留一個類屬性預設，from_env 以外的建構路徑（測試、腳本）就會靜默拿到它。"""
    import dataclasses

    field = next(f for f in dataclasses.fields(ApiConfig) if f.name == "state_base")
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING


def test_leaders_path_is_required_and_has_no_silent_default():
    """⭐⭐⭐ 漏設 FILET_LEADERS_PATH → **拒絕啟動**（同 FILET_EXCHANGE_DIR／
    FILET_STATE_BASE 的處理；這是同一個失敗模式的第三次，2026-07-20）。

    白名單有**五個**消費端各推導一次路徑：本 API（客戶能選誰）、引擎每輪的二次驗證
    （已在跟的人還能不能繼續）、activate CLI 的硬閘（管理端核可誰）、以及 leaderboard
    與 perf-series 兩個快照 timer（抓誰）。舊版每一端都是
    `env.get(...) or DEFAULT_LEADERS_PATH`，於是實機上 `filet-api.service` **根本沒有
    宣告這個變數**卻能正常運作——只因為它的 CWD 恰好是 repo 根，預設值又錨定 repo 根。
    那是巧合不是強制：白名單一旦搬家（或 WorkingDirectory 一改），API 與引擎就讀
    **不同的白名單**，而危險方向是 fail-open——管理端在引擎那份撤銷了一個 leader，
    目錄頁仍列著他、客戶仍選得到。「起不來」刻意優先於「起來了但兩邊讀不同檔」。
    """
    with pytest.raises(ValueError, match="FILET_LEADERS_PATH"):
        ApiConfig.from_env(_env(FILET_LEADERS_PATH=None))


def test_leaders_path_has_no_class_level_default_either():
    """⭐ 連 dataclass 層都沒有預設值（沿 exchange_dir／state_base 的同一個決定）：
    留一個類屬性預設，from_env 以外的建構路徑（測試、腳本）就會靜默拿到它——
    而對白名單來說「靜默拿到的那一份」就是**沒有把關**。"""
    import dataclasses

    field = next(f for f in dataclasses.fields(ApiConfig) if f.name == "leaders_path")
    assert field.default is dataclasses.MISSING
    assert field.default_factory is dataclasses.MISSING


def test_leaders_path_must_be_absolute():
    """⭐ 相對路徑一律拒絕：這是舊預設值當初被改成絕對路徑的理由（I4）。

    移除預設值時若沒把這個不變式一起搬進 require_leaders_path，等於用一個新洞
    （API 的 CWD 一漂移就驗到別的檔）換掉舊洞。
    """
    with pytest.raises(ValueError, match="絕對路徑"):
        ApiConfig.from_env(_env(FILET_LEADERS_PATH="var/filet/leaders.json"))


def test_leaders_path_used_verbatim_from_env():
    """API 讀到的就是 env 指的那一份——不做任何「猜一個更合理的位置」的加工。"""
    cfg = ApiConfig.from_env(_env(FILET_LEADERS_PATH="/etc/filet/curated.json"))
    assert cfg.leaders_path == "/etc/filet/curated.json"


def test_leader_changes_path_is_anchored_on_the_exchange_dir_not_pending():
    """⭐⭐ 記錄檔錨在**專屬交換目錄**，不是 API 私有的 pending.json 所在目錄。

    共享產物錨在私有產物身上會逼兩者同目錄，而該目錄的權限只能滿足其中一方
    （pending.json 含活化前的客戶資料，只有 filet-api 該讀得到；記錄檔則必須讓
    引擎讀得到）——這正是 C3 的根因。
    """
    cfg = ApiConfig.from_env(_env(FILET_PENDING_PATH="/var/lib/filet-api/pending.json",
                                  FILET_EXCHANGE_DIR="/var/lib/filet-exchange"))
    assert cfg.leader_changes_path == "/var/lib/filet-exchange/leader_changes.json"
    assert Path(cfg.leader_changes_path).parent != Path(cfg.pending_path).parent


def test_admin_addresses_optional_and_normalized():
    cfg = ApiConfig.from_env(_env(FILET_ADMIN_ADDRESSES=None))
    assert cfg.admin_addresses == frozenset()
    cfg2 = ApiConfig.from_env(_env(FILET_ADMIN_ADDRESSES="0x" + "AD" * 20))
    assert cfg2.admin_addresses == frozenset({"0x" + "ad" * 20})


def test_ops_paths_default_and_env_override():
    """營運後台唯讀資料源：未設 env 走預設（沿引擎既有 var/filet 慣例）。"""
    cfg = ApiConfig.from_env(_env())
    assert cfg.followers_path == "var/filet/followers.json"
    assert cfg.accrued_history_path == "var/copytrade/accrued_history.jsonl"
    cfg2 = ApiConfig.from_env(_env(FILET_FOLLOWERS_PATH="/x/f.json",
                                   FILET_ACCRUED_HISTORY_PATH="/x/h.jsonl"))
    assert cfg2.followers_path == "/x/f.json"
    assert cfg2.accrued_history_path == "/x/h.jsonl"


def test_followers_path_falls_back_to_engine_env():
    """同一份 manifest 兩個 env 名是誤設溫床：回退吃引擎既有的 FILET_FOLLOWERS
    （scripts/filet_daily_report.py、panic_all.py 用的變數）；兩者同設時新名優先。"""
    cfg = ApiConfig.from_env(_env(FILET_FOLLOWERS="/engine/f.json"))
    assert cfg.followers_path == "/engine/f.json"
    cfg2 = ApiConfig.from_env(_env(FILET_FOLLOWERS="/engine/f.json",
                                   FILET_FOLLOWERS_PATH="/api/f.json"))
    assert cfg2.followers_path == "/api/f.json"


def test_constants_single_source():
    """opus 審 M4：門檻與費率上限不重新宣告字面量，直接引用 spark.config 既有常數。"""
    from spark.config import MIN_BUILDER_BALANCE, Settings
    cfg = ApiConfig.from_env(_env())
    assert cfg.max_fee_rate == Settings.max_rate
    assert cfg.min_user_deposit is MIN_BUILDER_BALANCE
    assert cfg.min_builder_balance is MIN_BUILDER_BALANCE


# ---------- stripe 設定（M3 計費骨幹） ----------
# 注意：沿用檔頭既有 _env()（非計畫文字裡另一份同名 helper）——本檔已有模組級
# _env，重新 def 會覆蓋前面所有測試呼叫的版本（覆蓋後少了 FILET_ADMIN_ADDRESSES
# 預設值，會炸掉 test_admin_addresses_optional_and_normalized）。既有 _env 的
# 預設欄位對 stripe 測試同樣夠用，故直接複用。


def test_stripe_unset_means_billing_disabled():
    cfg = ApiConfig.from_env(_env())
    assert cfg.stripe_secret_key is None
    assert cfg.stripe_webhook_secret is None
    assert cfg.stripe_price_id is None
    assert cfg.billing_enabled is False


def test_stripe_full_set_enables_billing():
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_abc",
                                  FILET_STRIPE_WEBHOOK_SECRET="whsec_x",
                                  FILET_STRIPE_PRICE_ID="price_x"))
    assert cfg.billing_enabled is True
    assert cfg.stripe_price_id == "price_x"


def test_stripe_partial_set_refuses_startup():
    """三個一起設或都不設——半開狀態（如漏 webhook secret）直接拒啟動（設計定案 2）。"""
    with pytest.raises(ValueError, match="Stripe"):
        ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_abc"))


def test_live_key_refused_at_startup():
    """⭐ 紅線 1：非 sk_test_ 前綴（含 sk_live_）直接拒啟動——真實收費是人工決策。"""
    with pytest.raises(ValueError, match="sk_test_"):
        ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_live_abc",
                                FILET_STRIPE_WEBHOOK_SECRET="whsec_x",
                                FILET_STRIPE_PRICE_ID="price_x"))


def test_live_key_refused_on_direct_construction():
    """⭐ 結構性：不經 from_env 直接建構 ApiConfig 也擋（__post_init__）。"""
    with pytest.raises(ValueError, match="sk_test_"):
        ApiConfig(network="testnet", builder_address="0x" + "b1" * 20,
                  siwe_domain="d", siwe_uri="https://d", db_path="x.db",
                  keysvc_sock="x.sock", pending_path="p.json",
                  exchange_dir="/tmp/filet-exchange", state_base="/opt/filet/state",
                  leaders_path="/opt/filet/spark/var/filet/leaders.json",
                  admin_addresses=frozenset(),
                  stripe_secret_key="sk_live_abc",
                  stripe_webhook_secret="whsec_x", stripe_price_id="price_x")


def test_partial_set_refused_on_direct_construction():
    """⭐ opus Finding 2：半開狀態（只設 key、缺 webhook secret/price）在直接建構
    路徑也拒——__post_init__ 三元組驗證，不只 from_env。"""
    with pytest.raises(ValueError, match="Stripe"):
        ApiConfig(network="testnet", builder_address="0x" + "b1" * 20,
                  siwe_domain="d", siwe_uri="https://d", db_path="x.db",
                  keysvc_sock="x.sock", pending_path="p.json",
                  exchange_dir="/tmp/filet-exchange", state_base="/opt/filet/state",
                  leaders_path="/opt/filet/spark/var/filet/leaders.json",
                  admin_addresses=frozenset(),
                  stripe_secret_key="sk_test_abc")


def test_price_display_is_optional_and_not_part_of_trio():
    """⭐ price_display 只是顯示字串，**不納入**三元組的同設或同缺驗證：
    三元組齊全但沒設它 → 照常啟動、值為 None（前端顯示「價格待定」）。
    價格數字使用者尚未拍板 → 走設定，不寫死在程式碼。"""
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_abc",
                                  FILET_STRIPE_WEBHOOK_SECRET="whsec_x",
                                  FILET_STRIPE_PRICE_ID="price_x"))
    assert cfg.stripe_price_display is None
    assert cfg.billing_enabled is True
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_abc",
                                  FILET_STRIPE_WEBHOOK_SECRET="whsec_x",
                                  FILET_STRIPE_PRICE_ID="price_x",
                                  FILET_STRIPE_PRICE_DISPLAY="$29 / 月"))
    assert cfg.stripe_price_display == "$29 / 月"


def test_price_display_alone_does_not_enable_billing():
    """只設顯示字串（無三元組）不得被當成「設了 stripe」而擋下啟動——
    它不是金流設定的一員。"""
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_PRICE_DISPLAY="$29 / 月"))
    assert cfg.billing_enabled is False
    assert cfg.stripe_price_display == "$29 / 月"


def test_key_not_in_config_repr():
    """secret 不進 repr/log（縱深防禦；dataclass 預設 repr 會印全部欄位——必須遮）。"""
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_secret123",
                                  FILET_STRIPE_WEBHOOK_SECRET="whsec_secret456",
                                  FILET_STRIPE_PRICE_ID="price_x"))
    assert "sk_test_secret123" not in repr(cfg)
    assert "whsec_secret456" not in repr(cfg)
