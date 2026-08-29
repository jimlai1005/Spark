"""tests/test_filet_auto_activate_leverage_guard.py
Opus 審查 Warning 4：`scripts/filet_auto_activate._resolve_leverage_cap` 對
`leaders.json` 的 `max_leverage` 欄位若是非正值（`-1`／`0`）或不合法字串
（`"abc"`），過去照樣塞進 `min(20, parsed)`——`min(20, -1) == -1`／
`min(20, 0) == 0`，vault 保護帽被一個手誤（或惡意）欄位放寬到「幾乎任何槓桿
都通過」，方向與整條函式「不放寬 vault 保護語意」直接相反。

本檔驗證：非正值／不合法字串一律視同「未設」，vault leader 的
`COPY_MAX_TARGET_LEVERAGE` 恆維持既有的 20x 帽，不被縮小（此處『縮小』指
`min()` 因負值/零而挑到那個荒謬值，實際效果是保護被放寬，術語沿用審查原文
「不得被非正值放寬」）。

沿 `test_filet_auto_activate.py` 既有的 `_Site` 現場與 `site`/`_env_kv`/
`_make_leader_max_leverage` fixture（同一組全離線真密碼學基座，不重複定義，
避免兩份 fixture 漂移）。
"""
import pytest

from tests.test_filet_auto_activate import (_env_kv, _LEADER,
                                             _make_leader_max_leverage, site)  # noqa: F401

_VAULT_DEFAULT_CAP = "20"


@pytest.mark.parametrize("bad_max_leverage", ["-1", "0", "abc"])
def test_vault_leader_bad_max_leverage_does_not_widen_cap(site, bad_max_leverage):  # noqa: F811
    _make_leader_max_leverage(site, max_leverage=bad_max_leverage, kind="vault")
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert _env_kv(site)["COPY_MAX_TARGET_LEVERAGE"] == _VAULT_DEFAULT_CAP


@pytest.mark.parametrize("bad_max_leverage", ["-1", "0", "abc"])
def test_standard_leader_bad_max_leverage_is_not_injected(site, bad_max_leverage):  # noqa: F811
    """standard leader（非 vault）沒有 20x 保底帽可退——非正值/不合法字串必須
    視同未設，不注入任何 `COPY_MAX_TARGET_LEVERAGE`（不能把 -1/0 這種荒謬值
    直接塞進引擎環境變數）。"""
    _make_leader_max_leverage(site, max_leverage=bad_max_leverage)
    site.sign_change(leader=_LEADER)
    assert site.run() == 0
    assert "COPY_MAX_TARGET_LEVERAGE" not in _env_kv(site)
