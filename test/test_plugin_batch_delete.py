"""批量删除未启用插件的回归测试。

v0.6.6 新增“一键删除所有未启用的插件”：
- 只删除 plugins_info.enabled=False 且不在 enabled_plugins 列表中的插件；
- 已启用的插件必须保留（配置、文件都不动）；
- 一次性回写配置、删除文件，最后只 reload 一次。
"""

import json
import logging
import os

from xiaomusic.js_plugin_manager import JSPluginManager


def _make_manager(tmp_path, monkeypatch, plugins):
    """构造不启动 Node 的 JSPluginManager。plugins: [(name, enabled), ...]"""
    mgr = object.__new__(JSPluginManager)
    mgr.xiaomusic = None
    mgr.log = logging.getLogger("test")
    mgr.plugins_dir = str(tmp_path / "js_plugins")
    mgr.plugins_config_path = str(tmp_path / "plugins-config.json")
    mgr.plugins = {}
    mgr._config_cache = None
    mgr._config_cache_time = 0
    mgr._config_cache_ttl = 180

    os.makedirs(mgr.plugins_dir, exist_ok=True)
    for name, _enabled in plugins:
        with open(os.path.join(mgr.plugins_dir, f"{name}.js"), "w", encoding="utf-8") as f:
            f.write("module.exports = {};")

    config = {
        "music_free_info": {
            "plugins_info": [
                {"name": n, "file": f"{n}.js", "enabled": e} for n, e in plugins
            ],
            "enabled_plugins": [n for n, e in plugins if e],
        }
    }
    with open(mgr.plugins_config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)

    monkeypatch.setattr(mgr, "reload_plugins", lambda: None)
    monkeypatch.setattr(mgr, "_invalidate_config_cache", lambda: None)
    return mgr


def _cfg(mgr):
    with open(mgr.plugins_config_path, encoding="utf-8") as f:
        return json.load(f)["music_free_info"]


def test_batch_delete_only_removes_disabled(tmp_path, monkeypatch):
    """3 个插件中 1 个启用、2 个禁用：只删 2 个禁用的，启用项保留。"""
    mgr = _make_manager(
        tmp_path,
        monkeypatch,
        [("keep_enabled", True), ("remove_a", False), ("remove_b", False)],
    )

    result = mgr.uninstall_all_disabled_plugins()

    assert result["success"] is True
    assert result["count"] == 2
    assert set(result["removed"]) == {"remove_a", "remove_b"}

    cfg = _cfg(mgr)
    names = [p["name"] for p in cfg["plugins_info"]]
    assert names == ["keep_enabled"]
    assert cfg["enabled_plugins"] == ["keep_enabled"]
    assert os.path.exists(os.path.join(mgr.plugins_dir, "keep_enabled.js"))
    assert not os.path.exists(os.path.join(mgr.plugins_dir, "remove_a.js"))
    assert not os.path.exists(os.path.join(mgr.plugins_dir, "remove_b.js"))


def test_batch_delete_all_disabled(tmp_path, monkeypatch):
    """全部禁用时全部删除，返回正确的计数。"""
    mgr = _make_manager(tmp_path, monkeypatch, [("a", False), ("b", False)])

    result = mgr.uninstall_all_disabled_plugins()

    assert result["success"] is True
    assert result["count"] == 2
    assert _cfg(mgr)["plugins_info"] == []
    assert _cfg(mgr)["enabled_plugins"] == []
    assert os.listdir(mgr.plugins_dir) == []


def test_batch_delete_none_disabled(tmp_path, monkeypatch):
    """全部启用时不应删除任何插件。"""
    mgr = _make_manager(tmp_path, monkeypatch, [("a", True), ("b", True)])

    result = mgr.uninstall_all_disabled_plugins()

    assert result["success"] is True
    assert result["count"] == 0
    assert result["removed"] == []
    assert len(_cfg(mgr)["plugins_info"]) == 2
    assert len(os.listdir(mgr.plugins_dir)) == 2


def test_batch_delete_cleans_stale_enabled_entry(tmp_path, monkeypatch):
    """enabled_plugins 里残留的禁用项（状态不一致）也应被清理，禁止误删启用项。"""
    mgr = _make_manager(tmp_path, monkeypatch, [("enabled_one", True), ("stale", False)])
    # 模拟历史遗留状态：stale 既标记 enabled=False，又残留在 enabled_plugins 中
    cfg = _cfg(mgr)
    cfg["enabled_plugins"].append("stale")
    with open(mgr.plugins_config_path, "w", encoding="utf-8") as f:
        json.dump({"music_free_info": cfg}, f, ensure_ascii=False)

    result = mgr.uninstall_all_disabled_plugins()

    # 判定以 plugins_info.enabled 为准，stale 仍应被删掉
    assert result["success"] is True
    assert result["count"] == 1
    assert "stale" in result["removed"]
    cfg_after = _cfg(mgr)
    assert cfg_after["enabled_plugins"] == ["enabled_one"]
    assert [p["name"] for p in cfg_after["plugins_info"]] == ["enabled_one"]
