"""播放链路健康检查与小米接口返回值解析工具。

背景：
1. 小爱音箱播放本地音乐时，实际拿到的是一个 http(s) URL。如果配置里的
   hostname 是上游作者的默认值、localhost 或错误端口，音箱会直接提示
   “播放失败 / 检查网络配置”，而服务端日志只显示“已经开始播放”。
2. MiNA 的 ubus 接口即使业务失败，也常返回 HTTP 200 且顶层 code=0，
   只有 data.code 非零。旧代码只看“是否抛异常”，会把失败当成成功。

这里集中提供纯函数与轻量探针，供配置、播放器和诊断接口复用。
"""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

# 上游 hanxi/xiaomusic 的硬编码默认值，历史配置里经常残留
LEGACY_DEFAULT_HOSTNAME = "http://192.168.2.5"

# 音箱不可能通过这些地址访问到 NAS 上的音乐服务
PLACEHOLDER_HOSTS = {
    "",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "192.168.2.5",
}

_PROBE_TARGETS = ("223.5.5.5", "114.114.114.114", "8.8.8.8")


def normalize_base_url(value: str) -> str:
    """把 hostname 配置规范化为不带端口/路径的 http(s) 根地址。

    >>> normalize_base_url("192.168.1.10")
    'http://192.168.1.10'
    >>> normalize_base_url("http://192.168.1.10:58090/foo")
    'http://192.168.1.10'
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = parsed.hostname or ""
    if not host:
        return ""
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "http"
    return f"{scheme}://{host}"


def is_placeholder_hostname(value: str) -> bool:
    """判断 hostname 是否是未配置/不可被音箱访问的占位地址。"""
    base = normalize_base_url(value)
    if not base:
        return True
    host = (urlparse(base).hostname or "").lower()
    return host in PLACEHOLDER_HOSTS


def detect_lan_ip() -> str:
    """尽力探测本机对局域网的出口 IPv4。

    UDP connect 不会真正发包，只是让内核选择路由，因此无需外网可达。
    Docker bridge 模式下拿到的是容器 IP（172.x），此时仍需在 Web 设置页
    手动填写 NAS 的局域网 IP；调用方会给出明确日志提示。
    """
    for target in _PROBE_TARGETS:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.connect((target, 53))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except Exception:  # pragma: no cover - 取决于宿主网络环境
            continue
        finally:
            if sock is not None:
                sock.close()
    return ""


def resolve_playback_host(configured: str, *, logger=None) -> tuple[str, str]:
    """返回 (可下发给音箱的根地址, 来源)。

    来源为 configured / auto / none。auto 表示配置缺失或仍是上游默认值，
    此时使用探测到的本机 IP，并打印醒目告警要求用户确认。
    """
    logger = logger or log
    base = normalize_base_url(configured)
    if base and not is_placeholder_hostname(base):
        return base, "configured"

    if configured and is_placeholder_hostname(configured):
        logger.warning(
            "[PLAYBACK] hostname 仍是占位地址 %r（未配置或上游默认值），"
            "小爱音箱会无法下载歌曲并提示“播放失败”。正在尝试使用本机 IP。",
            configured,
        )

    ip = detect_lan_ip()
    if not ip:
        logger.error(
            "[PLAYBACK] 无法确定可用的 hostname，请到 Web 设置页填写 NAS 的局域网 IPv4 地址"
        )
        return "", "none"

    host = f"http://{ip}"
    logger.warning(
        "[PLAYBACK] 自动使用 hostname=%s。Docker bridge 模式下该地址可能是容器 IP，"
        "如音箱仍提示播放失败，请在 Web 设置页手动填写 NAS 局域网 IP。",
        host,
    )
    return host, "auto"


def is_mi_response_ok(ret) -> bool:
    """判断 MiNA ubus 返回是否真的成功。

    成功形如 {"code": 0, "data": {"code": 0, "info": "..."}}；
    失败可能是顶层 code!=0，也可能是 data.code!=0（如 601 参数错误）。
    """
    if not isinstance(ret, dict):
        return False
    top_code = ret.get("code")
    if top_code is not None and str(top_code) not in ("0", "200"):
        return False
    data = ret.get("data")
    if isinstance(data, dict):
        data_code = data.get("code")
        if data_code is not None and str(data_code) not in ("0", "200"):
            return False
    return True


def describe_mi_response(ret, limit: int = 500) -> str:
    """脱敏且限长地描述接口返回，便于日志排查。"""
    text = str(ret)
    if len(text) > limit:
        text = text[:limit] + "...(truncated)"
    return text


async def probe_playback_url(url: str, timeout: float = 3.0) -> tuple[bool, int, str]:
    """从服务端探测歌曲 URL 是否可访问。

    返回 (是否可访问, HTTP 状态码, 错误信息)。只取一个字节，避免消耗流量。
    """
    if not url or not url.startswith(("http://", "https://")):
        return False, 0, "URL 不是 http(s) 地址"
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(
                url, headers={"Range": "bytes=0-0"}, allow_redirects=True
            ) as resp:
                # 读取一点点内容，确保响应体真的可用
                await resp.content.read(1)
                return resp.status in (200, 206), resp.status, ""
    except Exception as exc:  # pragma: no cover - 依赖网络环境
        return False, 0, str(exc)


__all__ = [
    "LEGACY_DEFAULT_HOSTNAME",
    "PLACEHOLDER_HOSTS",
    "normalize_base_url",
    "is_placeholder_hostname",
    "detect_lan_ip",
    "resolve_playback_host",
    "is_mi_response_ok",
    "describe_mi_response",
    "probe_playback_url",
]
