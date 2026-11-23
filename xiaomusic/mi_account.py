"""加固版小米账号实现。

miservice 自带的 MiAccount 有两个会在实际部署中造成“连上几分钟就掉线”的问题：

1. 当登录请求已经携带 passToken 时，小米服务端有时不再返回 passToken/nonce/
   ssecurity 等字段，原实现直接读取 ``resp["passToken"]`` 会抛 KeyError，
   随后把内存 token 清空并删除 ``.mi.token``，导致所有服务同时失效。
2. 项目只登录了 ``micoapi``，而 MiIOService（TTS、部分型号控制）使用的是
   ``xiaomiio`` 服务票据。第一次调用时触发的隐式登录一旦失败，同样会删除
   ``.mi.token``。

本模块在依赖原有协议实现的基础上补齐了这两处缺口：登录时容错合并旧 token、
失败时不再删除本地票据、401 时按 sid 精确刷新服务票据，并支持在启动时预取
多个 sid。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from miservice import MiAccount
from miservice.miaccount import get_random

LOGGER = logging.getLogger(__name__)

SERVICE_MICOAPI = "micoapi"
SERVICE_XIAOMIIO = "xiaomiio"

# 小米登录响应中可能缺失、但可以从已有 token 继承的字段
_TOKEN_FIELDS = ("userId", "passToken", "nonce", "ssecurity", "location", "deviceId")
# 需要用来换取 serviceToken 的字段
_SERVICE_TOKEN_FIELDS = ("location", "nonce", "ssecurity")


class XiaomiAuthError(Exception):
    """小米登录态不可用。"""


def get_service_token(token: dict | None, sid: str) -> str:
    """安全取出指定 sid 的 serviceToken。"""
    value = (token or {}).get(sid)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[1] or ""
    return ""


def build_token(
    auth_data: dict | None,
    *,
    existing: dict | None = None,
    cookie_data: dict | None = None,
    device_id: str | None = None,
) -> dict | None:
    """把 auth.json / cookie / .mi.token 合并成 Miservice 可用的 token。

    auth.json（扫码登录产生）里只有一份 serviceToken，通常属于 mijia；
    .mi.token 里保存的则是 micoapi / xiaomiio 各自的 serviceToken。两者
    合并后，首次启动就无需再通过密码登录换取第二份票据。
    """
    base: dict[str, Any] = dict(existing or {})

    for source in (auth_data or {}, cookie_data or {}):
        for key in ("userId", "passToken", "cUserId", "nonce", "ssecurity", "location"):
            if source.get(key):
                base[key] = source[key]
        source_device_id = source.get("deviceId") or source.get("PassportDeviceId")
        if source_device_id:
            base["deviceId"] = source_device_id
        # 扫码登录文件里的 serviceToken 先暂存，等确认 sid 后再放进对应字段
        if source.get("serviceToken"):
            base.setdefault("_fallback_service_token", source["serviceToken"])

    if device_id:
        base["deviceId"] = device_id
    base.setdefault("deviceId", get_random(16).upper())

    fallback_service_token = base.pop("_fallback_service_token", "")
    ssecurity = base.get("ssecurity")
    for sid in (SERVICE_MICOAPI, SERVICE_XIAOMIIO):
        if get_service_token(base, sid):
            continue
        if ssecurity and fallback_service_token:
            base[sid] = (ssecurity, fallback_service_token)

    if not base.get("userId") or not base.get("passToken"):
        # 没有基础凭据时，保留已保存的 sid 票据也仍有意义，交由 login() 判断
        return base or None
    return base


class RobustMiAccount(MiAccount):
    """不会因一次失败登录而丢失本地票据的 MiAccount。"""

    def __init__(
        self,
        session,
        username,
        password,
        token_store=None,
        *,
        token_provider: Callable[[], dict | None] | None = None,
        log=None,
    ):
        super().__init__(session, username, password, token_store)
        self._token_provider = token_provider
        self._log = log or LOGGER
        self._login_lock = asyncio.Lock()
        self.last_login_error = ""
        self.last_login_at: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 登录
    # ------------------------------------------------------------------ #
    async def login(self, sid: str, force_refresh: bool = False) -> bool:
        async with self._login_lock:
            return await self._login_locked(sid, force_refresh=force_refresh)

    def _restore_token(self) -> dict:
        """token 被异常清空后，从配置/文件恢复基础凭据。"""
        if self.token and self.token.get("passToken"):
            return dict(self.token)
        if self._token_provider:
            try:
                restored = self._token_provider()
            except Exception as e:  # pragma: no cover - 仅记录，不影响主流程
                self._log.warning("[MI-AUTH] 读取本地 token 失败: %s", e)
                restored = None
            if restored and restored.get("passToken"):
                self.token = dict(restored)
                self._log.info("[MI-AUTH] 已从本地凭据恢复登录态")
                return dict(self.token)
        return dict(self.token or {})

    async def _login_locked(self, sid: str, force_refresh: bool = False) -> bool:
        original_token = self._restore_token()
        if not self.token:
            self.token = {"deviceId": get_random(16).upper()}

        try:
            resp = await self._service_login(f"serviceLogin?sid={sid}&_json=true")

            has_service_fields = all(resp.get(k) for k in _SERVICE_TOKEN_FIELDS)
            has_cached_service_token = bool(get_service_token(original_token, sid))
            need_password = (
                resp.get("code") != 0
                or (force_refresh and not has_service_fields)
                or (not has_service_fields and not has_cached_service_token)
            )

            if need_password:
                if not self.username or not self.password:
                    raise XiaomiAuthError(
                        "登录响应缺少票据信息，且没有账号密码可用于重新认证；"
                        "请检查账号密码或重新扫码登录"
                    )
                data = {
                    "_json": "true",
                    "qs": resp.get("qs", ""),
                    "sid": resp.get("sid", sid),
                    "_sign": resp.get("_sign", ""),
                    "callback": resp.get("callback", ""),
                    "user": self.username,
                    "hash": hashlib.md5(self.password.encode()).hexdigest().upper(),
                }
                # 关键：显式带空 passToken，强制服务端返回完整的票据字段
                resp = await self._service_login(
                    "serviceLoginAuth2", data, force_empty_pass=True
                )
                if resp.get("code") != 0:
                    raise XiaomiAuthError(f"小米账号认证失败: {resp}")
                # 密码认证后会下发完整票据，以最终响应为准重新判断
                has_service_fields = all(resp.get(k) for k in _SERVICE_TOKEN_FIELDS)

            token = dict(original_token)
            for key in _TOKEN_FIELDS:
                if resp.get(key):
                    token[key] = resp[key]
            if not token.get("userId") or not token.get("passToken"):
                raise XiaomiAuthError("登录响应缺少 userId/passToken")

            if has_service_fields:
                service_token = await self._security_token_service(
                    resp["location"], resp["nonce"], resp["ssecurity"]
                )
            else:
                # 服务端复用了现有 passToken 而没有下发新票据，沿用缓存票据
                service_token = get_service_token(original_token, sid)
            if not service_token:
                raise XiaomiAuthError(f"无法获取 {sid} 的 serviceToken")

            token[sid] = (token.get("ssecurity") or original_token.get("ssecurity"), service_token)
            self.token = token
            self.last_login_error = ""
            self.last_login_at[sid] = time.time()
            if self.token_store:
                self.token_store.save_token(self.token)
            self._log.info("[MI-AUTH] %s 登录成功", sid)
            return True
        except Exception as e:
            # 绝不因为一次失败就清空 .mi.token：否则 micoapi 也会被连带踢下线
            self.token = original_token or None
            self.last_login_error = str(e)
            self._log.warning("[MI-AUTH] %s 登录失败: %s", sid, e)
            return False

    async def _service_login(self, uri, data=None, force_empty_pass=False):
        """与 Miservice 同协议，但可以强制不带 passToken 请求完整票据。"""
        self.now_ua = self.ua.random
        headers = {"User-Agent": self.now_ua}
        token = self.token or {}
        cookies = {
            "sdkVersion": "3.9",
            "deviceId": token.get("deviceId") or get_random(16).upper(),
        }
        if force_empty_pass or not token.get("passToken"):
            cookies["passToken"] = ""
        else:
            cookies["userId"] = token["userId"]
            cookies["passToken"] = token["passToken"]

        url = "https://account.xiaomi.com/pass/" + uri
        async with self.session.request(
            "GET" if data is None else "POST",
            url,
            data=data,
            cookies=cookies,
            headers=headers,
            ssl=False,
        ) as r:
            raw = await r.read()
        return json.loads(raw[11:])

    async def _security_token_service(self, location, nonce, ssecurity):
        # 复用父类实现，避免重复协议细节
        return await super()._securityTokenService(location, nonce, ssecurity)

    # ------------------------------------------------------------------ #
    # API 请求
    # ------------------------------------------------------------------ #
    async def mi_request(self, sid, url, data, headers, relogin=True):
        headers["User-Agent"] = self.now_ua
        if not self.token or sid not in self.token:
            if not await self.login(sid):
                raise Exception(f"Error {url}: Login failed")

        cookies = {
            "userId": self.token["userId"],
            "serviceToken": get_service_token(self.token, sid),
        }
        content = data(self.token, cookies) if callable(data) else data
        method = "GET" if data is None else "POST"
        async with self.session.request(
            method, url, data=content, cookies=cookies, headers=headers
        ) as r:
            status = r.status
            if status == 200:
                resp = await r.json(content_type=None)
                code = resp.get("code")
                if code == 0:
                    return resp
                if "auth" in str(resp.get("message", "")).lower() or code in (401, 70016):
                    status = 401
            else:
                resp = await r.text()

        if status in (401, 403) and relogin:
            self._log.warning("[MI-AUTH] %s 票据失效，刷新后重试: %s", sid, url)
            token = dict(self.token or {})
            token.pop(sid, None)  # 只丢弃失效的 sid 票据，保留 passToken
            self.token = token
            if await self.login(sid, force_refresh=True):
                return await self.mi_request(sid, url, data, headers, False)

        raise Exception(f"Error {url}: {resp}")

    # 方便外部诊断，不暴露敏感值
    def status(self) -> dict:
        token = self.token or {}
        return {
            "has_pass_token": bool(token.get("passToken")),
            "user_id": token.get("userId"),
            "device_id": token.get("deviceId"),
            "services": [
                {
                    "sid": sid,
                    "has_service_token": bool(get_service_token(token, sid)),
                    "last_login_at": self.last_login_at.get(sid),
                }
                for sid in (SERVICE_MICOAPI, SERVICE_XIAOMIIO)
            ],
            "last_error": self.last_login_error,
        }


__all__ = [
    "SERVICE_MICOAPI",
    "SERVICE_XIAOMIIO",
    "RobustMiAccount",
    "XiaomiAuthError",
    "build_token",
    "get_service_token",
]
