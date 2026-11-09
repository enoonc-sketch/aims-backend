"""Communication test service.

Used by the "Communication Management" (通讯管理) module to check whether a
gateway or device is actually reachable, rather than just showing a status
flag that nobody has verified recently.

Two levels of check, chosen automatically from the protocol:
- Generic TCP handshake (works for MQTT, OPC UA, HTTP, or any TCP service):
  proves the host/port is reachable and something is listening.
- Modbus TCP probe: a TCP handshake only proves *a* service answered on the
  port, not that it's actually Modbus. For Modbus TCP we send a minimal
  "Read Holding Registers" request and check for a well-formed Modbus
  response (including a Modbus *exception* reply, which still proves a real
  Modbus TCP server is on the other end).

All socket calls are blocking; call this from a sync FastAPI endpoint (or
`run_in_threadpool`) rather than an async one.
"""
from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Optional

# Conventional default ports, used only when the caller doesn't supply one.
_DEFAULT_PORTS = {
    "modbus tcp": 502,
    "modbus": 502,
    "mqtt": 1883,
    "opc ua": 4840,
    "opcua": 4840,
    "http": 80,
    "https": 443,
}
_FALLBACK_PORT = 502


def default_port_for(protocol: Optional[str]) -> int:
    return _DEFAULT_PORTS.get((protocol or "").strip().lower(), _FALLBACK_PORT)


@dataclass
class CommTestOutcome:
    success: bool
    latency_ms: Optional[float]
    message: str
    detail: Optional[str] = None


def _tcp_connect(host: str, port: int, timeout: float) -> tuple[bool, Optional[float], Optional[str]]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return True, (time.perf_counter() - start) * 1000, None
    except socket.timeout:
        return False, (time.perf_counter() - start) * 1000, "连接超时"
    except ConnectionRefusedError:
        return False, (time.perf_counter() - start) * 1000, "连接被拒绝（端口未开放或服务未启动）"
    except socket.gaierror:
        return False, None, "无法解析主机地址"
    except OSError as exc:
        return False, (time.perf_counter() - start) * 1000, f"网络错误：{exc}"


def _modbus_probe(
    host: str, port: int, timeout: float, unit_id: int = 1
) -> tuple[bool, Optional[float], Optional[str]]:
    start = time.perf_counter()
    transaction_id = 1
    # MBAP header (transaction id, protocol id=0, length) + unit id +
    # function code 0x03 (Read Holding Registers) + start addr 0, qty 1.
    request = struct.pack(">HHHBB", transaction_id, 0, 6, unit_id, 0x03) + struct.pack(">HH", 0, 1)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            resp = sock.recv(256)
        latency = (time.perf_counter() - start) * 1000
        if len(resp) >= 8 and resp[0:2] == struct.pack(">H", transaction_id):
            func_code = resp[7]
            if func_code == 0x03:
                return True, latency, None
            if func_code == 0x83:
                # Modbus *exception* response (e.g. illegal register address)
                # — the device rejected our probe request, but that still
                # proves a live Modbus TCP server answered it.
                return True, latency, "设备已响应（返回 Modbus 异常码，可能是寄存器地址不支持，属正常现象）"
        return False, latency, "端口已连接，但收到的不是有效的 Modbus TCP 报文"
    except socket.timeout:
        return False, (time.perf_counter() - start) * 1000, "连接或读取超时"
    except ConnectionRefusedError:
        return False, (time.perf_counter() - start) * 1000, "连接被拒绝（端口未开放或服务未启动）"
    except socket.gaierror:
        return False, None, "无法解析主机地址"
    except OSError as exc:
        return False, (time.perf_counter() - start) * 1000, f"网络错误：{exc}"


def run_test(
    host: Optional[str],
    port: Optional[int],
    protocol: Optional[str],
    timeout: float = 3.0,
) -> CommTestOutcome:
    """Run a communication test against host:port and return the outcome.

    `protocol` only affects which probe is used (Modbus gets the real
    Modbus-frame probe, everything else gets a plain TCP handshake) and
    which default port is assumed when `port` is omitted.
    """
    host = (host or "").strip()
    if not host:
        return CommTestOutcome(False, None, "缺少主机地址（IP）")

    resolved_port = port or default_port_for(protocol)
    is_modbus = "modbus" in (protocol or "").lower()

    if is_modbus:
        ok, latency, err = _modbus_probe(host, resolved_port, timeout)
    else:
        ok, latency, err = _tcp_connect(host, resolved_port, timeout)

    target = f"{host}:{resolved_port}"
    if ok:
        latency_txt = f"，耗时 {latency:.0f} ms" if latency is not None else ""
        return CommTestOutcome(True, round(latency, 1) if latency is not None else None,
                                f"通讯正常（{target}{latency_txt}）", err)

    return CommTestOutcome(False, round(latency, 1) if latency is not None else None,
                            f"通讯失败（{target}）：{err or '未知错误'}", err)
