"""插件启用/禁用/卸载死锁修复的回归测试。

旧实现要求 plugin_name 已在运行时表 self.plugins 中才允许启用/卸载，
但插件未加载时恰恰不在该表中，导致按钮永远失败。修复后以配置文件和
插件文件为准，不再依赖运行时加载状态。
"""

import json
import logging
import os

from xiaomusic.js_plugin_manager import JSPluginManager

log = logging.getLogger("test")


def _make_manager(tmp_path, monkeypatch, plugin_names):
    """构造一个不启动 Node 进程的 JSPluginManager，并写入测试用插件文件与配置。"""
    mgr = object.__new__(JSPluginManager)
    mgr.xiaomusic = None
    mgr.log = logging.getLogger("test")
    mgr.plugins_dir = str(tmp_path / "js_plugins")
    mgr.plugins_config_path = str(tmp_path / "plugins-config.json")
    mgr.plugins = {}  # 运行时表故意为空，模拟引擎重载后的状态
    mgr._config_cache = None
    mgr._config_cache_time = 0
    mgr._config_cache_ttl = 180

    os.makedirs(mgr.plugins_dir, exist_ok=True)
    for name in plugin_names:
        with open(os.path.join(mgr.plugins_dir, f"{name}.js"), "w", encoding="utf-8") as f:
            f.write("module.exports = {};")

    config = {
        "music_free_info": {
            "plugins_info": [
                {"name": n, "file": f"{n}.js", "enabled": False} for n in plugin_names
            ],
            "enabled_plugins": [],
        }
    }
    with open(mgr.plugins_config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)

    # reload 会启动/重启 Node 进程，测试里 mock 掉，只验证配置层行为
    monkeypatch.setattr(mgr, "reload_plugins", lambda: None)
    monkeypatch.setattr(mgr, "_invalidate_config_cache", lambda: None)
    return mgr


def _read_config(mgr):
    with open(mgr.plugins_config_path, encoding="utf-8") as f:
        return json.load(f)


def test_enable_plugin_when_not_in_runtime_table(tmp_path, monkeypatch):
    """插件不在运行时表时也必须能启用（死锁核心场景）。"""
    mgr = _make_manager(tmp_path, monkeypatch, ["gh_padao_fun"])
    assert "gh_padao_fun" not in mgr.plugins

    assert mgr.enable_plugin("gh_padao_fun") is True

    cfg = _read_config(mgr)["music_free_info"]
    assert "gh_padao_fun" in cfg["enabled_plugins"]
    assert next(p for p in cfg["plugins_info"] if p["name"] == "gh_padao_fun")["enabled"] is True


def test_uninstall_plugin_when_not_in_runtime_table(tmp_path, monkeypatch):
    """插件不在运行时表时也必须能卸载（用户报告的直接故障）。"""
    mgr = _make_manager(tmp_path, monkeypatch, ["gh_padao_fun"])
    assert "gh_padao_fun" not in mgr.plugins

    assert mgr.uninstall_plugin("gh_padao_fun") is True

    cfg = _read_config(mgr)["music_free_info"]
    assert "gh_padao_fun" not in cfg["enabled_plugins"]
    assert all(p["name"] != "gh_padao_fun" for p in cfg["plugins_info"])
    assert not os.path.exists(os.path.join(mgr.plugins_dir, "gh_padao_fun.js"))


def test_uninstall_then_enable_cycle(tmp_path, monkeypatch):
    """卸载后重新启用插件仍能工作，且配置一致。"""
    mgr = _make_manager(tmp_path, monkeypatch, ["cat_plugin"])
    assert mgr.uninstall_plugin("cat_plugin") is True
    # 重新造出插件文件，模拟重新下载
    with open(os.path.join(mgr.plugins_dir, "cat_plugin.js"), "w", encoding="utf-8") as f:
        f.write("module.exports = {};")
    assert mgr.enable_plugin("cat_plugin") is True
    assert "cat_plugin" in _read_config(mgr)["music_free_info"]["enabled_plugins"]


def test_enable_unknown_plugin_returns_false(tmp_path, monkeypatch):
    """不存在的插件启用应明确失败，而不是静默成功。"""
    mgr = _make_manager(tmp_path, monkeypatch, ["known"])
    assert mgr.enable_plugin("not_exist") is False


def test_uninstall_unknown_plugin_returns_false(tmp_path, monkeypatch):
    """配置与文件都不存在的插件卸载应返回 False。"""
    mgr = _make_manager(tmp_path, monkeypatch, ["known"])
    assert mgr.uninstall_plugin("not_exist") is False


def test_disable_plugin_updates_config(tmp_path, monkeypatch):
    """禁用插件应更新配置并从启用列表移除。"""
    mgr = _make_manager(tmp_path, monkeypatch, ["to_disable"])
    assert mgr.enable_plugin("to_disable") is True
    assert mgr.disable_plugin("to_disable") is True
    cfg = _read_config(mgr)["music_free_info"]
    assert "to_disable" not in cfg["enabled_plugins"]
    assert next(p for p in cfg["plugins_info"] if p["name"] == "to_disable")["enabled"] is False
