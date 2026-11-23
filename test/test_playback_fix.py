"""播放失败修复的回归测试：URL 规范化、接口返回值判断与自动回退。"""

import asyncio
import logging
from types import SimpleNamespace

from xiaomusic.device_player import XiaoMusicDevice
from xiaomusic.utils import playback_utils
from xiaomusic.utils.playback_utils import (
    is_mi_response_ok,
    is_placeholder_hostname,
    normalize_base_url,
)

log = logging.getLogger("test")


def test_normalize_base_url_strips_port_and_path():
    assert normalize_base_url("192.168.1.10") == "http://192.168.1.10"
    assert normalize_base_url("http://192.168.1.10:58090/foo") == "http://192.168.1.10"
    assert (
        normalize_base_url("https://music.example.com:443/")
        == "https://music.example.com"
    )
    assert normalize_base_url("  localhost ") == "http://localhost"
    assert normalize_base_url("") == ""


def test_placeholder_hostname_detection():
    assert is_placeholder_hostname("")
    assert is_placeholder_hostname("http://192.168.2.5")
    assert is_placeholder_hostname("127.0.0.1")
    assert is_placeholder_hostname("http://localhost")
    assert not is_placeholder_hostname("http://192.168.1.10")


def test_resolve_playback_host_uses_detected_ip_for_legacy_default(monkeypatch):
    monkeypatch.setattr(playback_utils, "detect_lan_ip", lambda: "192.168.1.23")
    host, source = playback_utils.resolve_playback_host("http://192.168.2.5")
    assert host == "http://192.168.1.23"
    assert source == "auto"

    host, source = playback_utils.resolve_playback_host("http://192.168.1.9")
    assert host == "http://192.168.1.9"
    assert source == "configured"


def test_mi_response_ok_checks_data_code():
    assert is_mi_response_ok({"code": 0, "data": {"code": 0, "info": "ok"}})
    assert is_mi_response_ok({"code": 0})
    assert not is_mi_response_ok({"code": 1, "message": "invalid admin"})
    assert not is_mi_response_ok({"code": 0, "data": {"code": 601}})
    assert not is_mi_response_ok(None)


class FakeMina:
    def __init__(self, fail_url=True):
        self.device2hardware = {}
        self.fail_url = fail_url
        self.calls = []

    async def play_by_url(self, device_id, url, _type=2):
        self.calls.append("player_play_url")
        if self.fail_url:
            return {"code": 0, "data": {"code": -1, "info": "播放失败"}}
        return {"code": 0, "data": {"code": 0}}

    async def play_by_music_url(self, device_id, url, _type=2, audio_id=""):
        self.calls.append("player_play_music")
        return {"code": 0, "data": {"code": 0}}


def _make_device(mina, fallback=True):
    device = XiaoMusicDevice.__new__(XiaoMusicDevice)
    device.config = SimpleNamespace(
        continue_play=False,
        use_music_api=False,
        playback_api_fallback=fallback,
        use_music_audio_id="12345",
    )
    device.auth_manager = SimpleNamespace(mina_service=mina)
    device.device = SimpleNamespace(hardware="TEST", did="did-1")
    device.device_id = "device-1"
    device.log = log
    device.last_play_error = ""
    device.last_play_api = ""

    async def _fake_audio_id(name):
        return "12345"

    device._get_audio_id = _fake_audio_id
    return device


def test_play_one_url_falls_back_when_mi_api_reports_failure():
    mina = FakeMina(fail_url=True)
    device = _make_device(mina)
    ret = asyncio.run(
        device.play_one_url("device-1", "http://192.168.1.2:8090/music/a.mp3", "a")
    )
    assert ret is not None
    assert mina.calls == ["player_play_url", "player_play_music"]
    assert device.last_play_api == "player_play_music"
    assert "TEST" in mina.device2hardware.values()


def test_play_one_url_reports_all_failures():
    class AllFail(FakeMina):
        async def play_by_music_url(self, device_id, url, _type=2, audio_id=""):
            self.calls.append("player_play_music")
            return {"code": 1, "message": "invalid admin"}

    mina = AllFail(fail_url=True)
    device = _make_device(mina)
    ret = asyncio.run(device.play_one_url("device-1", "http://x/a.mp3", "a"))
    assert ret is None
    assert device.last_play_error
