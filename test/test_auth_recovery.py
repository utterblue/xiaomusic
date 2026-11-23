"""小米登录态恢复相关回归测试。

覆盖 upstream 停止维护前长期存在的几个掉线场景：
- 复用 passToken 时服务端不下发 passToken，原实现 KeyError 并删除 .mi.token
- MiIOService 的 xiaomiio 票据未预取，运行时隐式登录失败连带清空 micoapi
- serviceToken 过期后 401，刷新时不应丢失 passToken
"""

import asyncio
import json
import logging

from miservice.miaccount import MiTokenStore

from xiaomusic.mi_account import (
    SERVICE_MICOAPI,
    SERVICE_XIAOMIIO,
    RobustMiAccount,
    build_token,
    get_service_token,
)

log = logging.getLogger("test")


class FakeCookie:
    def __init__(self, value):
        self.value = value


class FakeResponse:
    def __init__(self, status=200, payload=None, raw=None, cookies=None, text=""):
        self.status = status
        self.payload = payload or {}
        self.raw = raw
        self.cookies = cookies or {}
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        if self.raw is not None:
            return self.raw
        # miservice 固定跳过响应前 11 个字节（小米的 )]}' 前缀及填充）
        return b"01234567890" + json.dumps(self.payload).encode()

    async def json(self, content_type=None):
        return self.payload

    async def text(self):
        return self._text or json.dumps(self.payload)


class FakeSession:
    def __init__(self, responses=None, service_token="fresh-service-token"):
        self.responses = list(responses or [])
        self.calls = []
        self.service_token = service_token

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        return response

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(
            cookies={"serviceToken": FakeCookie(self.service_token)}, text="ok"
        )


def make_account(session, token_path, token=None, username="user", password="pass"):
    account = RobustMiAccount(session, username, password, str(token_path), log=log)
    account.token = dict(token) if token else None
    return account


def test_build_token_merges_auth_json_and_saved_service_tokens(tmp_path):
    saved = {
        "userId": "123",
        "passToken": "pass",
        "deviceId": "saved-device",
        SERVICE_MICOAPI: ("ss1", "micoapi-token"),
        SERVICE_XIAOMIIO: ("ss2", "xiaomiio-token"),
    }
    auth = {
        "userId": "123",
        "passToken": "pass",
        "deviceId": "auth-device",
        "ssecurity": "ss",
        "serviceToken": "fallback-token",
    }

    token = build_token(auth, existing=saved)

    assert token["deviceId"] == "auth-device"
    assert get_service_token(token, SERVICE_MICOAPI) == "micoapi-token"
    assert get_service_token(token, SERVICE_XIAOMIIO) == "xiaomiio-token"


def test_login_reuses_pass_token_when_server_omits_it(tmp_path):
    """issue #547: passToken 复用登录时响应缺字段，不得清空票据。"""
    token_path = tmp_path / ".mi.token"
    old_token = {
        "userId": "123",
        "passToken": "keep-me",
        "deviceId": "device",
        SERVICE_MICOAPI: ("ss", "old-service-token"),
    }
    MiTokenStore(str(token_path)).save_token(old_token)
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "code": 0,
                    "userId": "123",
                    "nonce": "nonce",
                    "ssecurity": "ss",
                    "location": "https://example.com/sts",
                    # 故意不返回 passToken
                }
            )
        ]
    )
    account = make_account(session, token_path, old_token)

    assert asyncio.run(account.login(SERVICE_MICOAPI)) is True
    assert account.token["passToken"] == "keep-me"
    assert get_service_token(account.token, SERVICE_MICOAPI) == "fresh-service-token"
    assert token_path.exists()
    assert json.loads(token_path.read_text())["passToken"] == "keep-me"


def test_login_reuses_cached_service_token_when_response_has_no_ticket_fields(tmp_path):
    token_path = tmp_path / ".mi.token"
    old_token = {
        "userId": "123",
        "passToken": "pass",
        "deviceId": "device",
        SERVICE_MICOAPI: ("ss", "cached-service-token"),
    }
    session = FakeSession([FakeResponse(payload={"code": 0, "userId": "123"})])
    account = make_account(session, token_path, old_token)

    assert asyncio.run(account.login(SERVICE_MICOAPI)) is True
    assert get_service_token(account.token, SERVICE_MICOAPI) == "cached-service-token"
    # 没有可用票据字段时不应调用换取 serviceToken 的接口
    assert not any("/sts" in call[1] for call in session.calls)


def test_failed_login_keeps_local_token_file(tmp_path):
    """认证失败时不允许删除 .mi.token，否则所有服务会一起掉线。"""
    token_path = tmp_path / ".mi.token"
    old_token = {
        "userId": "123",
        "passToken": "pass",
        "deviceId": "device",
        SERVICE_MICOAPI: ("ss", "service-token"),
    }
    MiTokenStore(str(token_path)).save_token(old_token)
    session = FakeSession(
        [
            FakeResponse(
                payload={
                    "code": 70016,
                    "description": "登录验证失败",
                    "qs": "qs",
                    "sid": SERVICE_MICOAPI,
                }
            )
        ]
    )
    account = make_account(session, token_path, old_token, username="", password="")

    assert asyncio.run(account.login(SERVICE_MICOAPI)) is False
    assert token_path.exists()
    assert account.token == old_token
    assert "登录" in account.last_login_error


def test_mi_request_refreshes_401_without_dropping_pass_token(tmp_path):
    token_path = tmp_path / ".mi.token"
    token = {
        "userId": "123",
        "passToken": "pass",
        "deviceId": "device",
        SERVICE_MICOAPI: ("ss", "expired-service-token"),
    }
    session = FakeSession(
        [
            FakeResponse(status=401, payload={"code": 401, "message": "auth failed"}),
            FakeResponse(
                payload={
                    "code": 0,
                    "userId": "123",
                    "nonce": "nonce",
                    "ssecurity": "ss",
                    "location": "https://example.com/sts",
                }
            ),
            FakeResponse(status=200, payload={"code": 0, "data": []}),
        ]
    )
    account = make_account(session, token_path, token)
    headers = {}

    result = asyncio.run(
        account.mi_request(
            SERVICE_MICOAPI,
            "https://api2.mina.mi.com/admin/v2/device_list",
            None,
            headers,
        )
    )

    assert result == {"code": 0, "data": []}
    assert account.token["passToken"] == "pass"
    assert get_service_token(account.token, SERVICE_MICOAPI) == "fresh-service-token"
    assert len([c for c in session.calls if "device_list" in c[1]]) == 2


def test_get_polling_cookies_uses_current_in_memory_token():
    from xiaomusic.auth import AuthManager

    manager = object.__new__(AuthManager)
    manager.config = type(
        "Config", (), {"cookie": "", "get_one_device_id": lambda self: "speaker-id"}
    )()
    account = type(
        "Account",
        (),
        {
            "token": {
                "userId": "123",
                SERVICE_MICOAPI: ("ss", "new-service-token"),
            }
        },
    )()
    manager.mina_service = type("Mina", (), {"account": account})()
    manager._load_token_file = lambda: None

    cookies = manager.get_polling_cookies("speaker-id")

    assert cookies["serviceToken"] == "new-service-token"
    assert cookies["userId"] == "123"
    assert cookies["deviceId"] == "speaker-id"
