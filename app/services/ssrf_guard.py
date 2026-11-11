"""SSRF / 内网探测防护（AIMS 1.0 阶段 1，审计项 M-3 / M-4）。

两套策略，对应两类不同的业务面：

1. `validate_public_url(url)` —— 外部厂商云平台抓取（external_scraper）。
   厂商平台是公网站点，URL 由用户配置。这里强制：
   - 仅允许 http/https scheme（封掉 file:// 等伪协议读本地文件）；
   - 域名解析后禁止环回 / 私网 / 链路本地 / 云元数据地址（169.254.169.254）。

2. `check_probe_target(db, host, port)` —— 通讯测试（/communication/test）。
   AIMS 的核心业务就是探测**局域网里的网关**，因此不能一刀切封私网。
   策略是：目标 host 已登记在网关表（gateways.ip_address）或设备 TAG 地址里
   → 放行（运维正当需求）；未登记的目标只允许公网地址——防止把
   "通讯测试"当成对内网任意主机的端口扫描 oracle。

解析后 IP 全部校验（hostname -> getaddrinfo 可能返回多个地址）。
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.device import DeviceComponent, Gateway


class SSRFError(ValueError):
    """URL / 探测目标未通过安全校验。"""


def _ip_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """非环回、非私网、非链路本地、非保留/未指定地址。"""
    return not (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """把主机名解析成 IP 列表（也可能是字面量 IP）。解析失败按 SSRFError 处理。"""
    try:
        infos = socket.getaddrinfo(host.strip(), None)
    except socket.gaierror as e:
        raise SSRFError(f"无法解析主机名: {host} ({e})") from None
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not out:
        raise SSRFError(f"主机名没有可用的 IP 地址: {host}")
    return out


def validate_public_url(url: str, *, field: str = "url") -> str:
    """外部抓取源 URL 校验：http/https + 解析后全部为公网地址。"""
    if not url or not url.strip():
        return url
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"{field} 只允许 http/https，当前 scheme: {parsed.scheme or '(空)'}")
    if not parsed.hostname:
        raise SSRFError(f"{field} 缺少主机名: {url}")
    for ip in resolve_host(parsed.hostname):
        if not _ip_is_public(ip):
            raise SSRFError(
                f"{field} 指向内网/保留地址（{ip}）。外部数据源必须配置公网厂商平台地址。"
            )
    return url


def _host_in_registered_assets(db: Session, host: str) -> bool:
    """host 是否已登记为网关地址或设备 TAG 地址（大小写不敏感）。"""
    needle = host.strip().lower()
    if not needle:
        return False
    # 逐条比较（地址可能带端口/大小写差异），网关与 TAG 都是小表
    rows = db.execute(
        select(Gateway.ip_address).where(Gateway.ip_address.is_not(None))
    ).all()
    for (ip,) in rows:
        if ip and needle in ip.strip().lower():
            return True
    rows = db.execute(
        select(DeviceComponent.tag_address).where(DeviceComponent.tag_address.is_not(None))
    ).all()
    for (addr,) in rows:
        if addr and needle in str(addr).strip().lower():
            return True
    return False


def check_probe_target(db: Session, host: str, port: int, *, registered_ok: bool = True) -> str:
    """通讯测试目标校验。返回规范化 host。

    - 已登记资产（网关 ip_address / TAG tag_address 含该 host）→ 放行；
    - 未登记目标 → 仅允许公网 IP（堵住内网任意主机端口扫描）。
    - 环回地址始终放行（本机自检是合法诊断场景）。
    """
    if not host or not host.strip():
        raise SSRFError("探测目标 host 不能为空")
    host = host.strip()
    if not (0 < int(port) < 65536):
        raise SSRFError(f"端口号不合法: {port}")

    # 常见的 "host:port" 误填——通讯测试的端口是独立参数
    if host.count(":") == 1 and ":" in host and not host.startswith("["):
        candidate, maybe_port = host.rsplit(":", 1)
        if maybe_port.isdigit():
            host = candidate

    if registered_ok and _host_in_registered_assets(db, host):
        return host

    # 环回 / 本机名：本机自检放行
    if host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return host

    for ip in resolve_host(host):
        if ip.is_loopback:
            return host
        if not _ip_is_public(ip):
            raise SSRFError(
                f"探测目标 {host}（{ip}）是内网地址，且未登记在任何网关或 TAG 上。"
                "请先在通讯管理里登记网关地址，再对它做连通性测试。"
            )
    return host
