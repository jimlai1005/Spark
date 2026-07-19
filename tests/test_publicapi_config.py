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
                  admin_addresses=frozenset(),
                  stripe_secret_key="sk_test_abc")


def test_key_not_in_config_repr():
    """secret 不進 repr/log（縱深防禦；dataclass 預設 repr 會印全部欄位——必須遮）。"""
    cfg = ApiConfig.from_env(_env(FILET_STRIPE_SECRET_KEY="sk_test_secret123",
                                  FILET_STRIPE_WEBHOOK_SECRET="whsec_secret456",
                                  FILET_STRIPE_PRICE_ID="price_x"))
    assert "sk_test_secret123" not in repr(cfg)
    assert "whsec_secret456" not in repr(cfg)
