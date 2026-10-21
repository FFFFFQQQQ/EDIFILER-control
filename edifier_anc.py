#!/usr/bin/env python3
"""EDIFIER W830NB 降噪控制(命令行版)。

协议要点(全部经实机验证):
  帧  = AA | EC | cmd | len_hi | len_lo | data... | crc
        EC = 0xEC (AppCode);len = len(data);crc = sum(所有前导字节) & 0xFF
  加密 = 载荷每个字节 XOR 0xA5
  查询 = cmd 0xCC,空载荷  -> 响应 data = [0xBE, 模式^0xA5, (db^0xA5)]
  设置 = cmd 0xC1,载荷 = XOR([groupIndex, 选项下标])

W830NB 的 groupIndex = 3,选项:
  1 深度降噪   2 舒适降噪   3 防风噪   4 环境声   5 降噪关

通道:BLE 服务 48098501-1a48-11e9-ab14-d663bd873d93
      写 48090002 / 收 48090001
      必须用「无响应写」(WriteWithoutResponse),带响应写会被 Windows 拒绝。
"""
from __future__ import annotations

import argparse
import asyncio
import sys

BLE_ADDR_DEFAULT = "C8:24:70:F4:A8:AC"
SVC = "48098501"
CH_NOTIFY = "48090001-1a48-11e9-ab14-d663bd873d93"
CH_WRITE = "48090002-1a48-11e9-ab14-d663bd873d93"

APP_CODE = 0xEC
XOR_KEY = 0xA5
GROUP_INDEX = 3

CMD_ANC_QUERY = 0xCC
CMD_ANC_SET = 0xC1
CMD_BATTERY = 0xD0
CMD_FIRMWARE = 0xC6

MODES = {
    "deep":     (1, "深度降噪"),
    "comfort":  (2, "舒适降噪"),
    "wide":     (3, "宽频"),
    "wind":     (4, "防风噪"),
    "ambient":  (5, "环境声"),
}


def build(cmd: int, data=None) -> bytes:
    """构造 V2 帧(载荷 XOR 0xA5)。"""
    d = [b ^ XOR_KEY for b in (data or [])]
    p = [0xAA, APP_CODE, cmd, (len(d) >> 8) & 0xFF, len(d) & 0xFF] + d
    return bytes(p + [sum(p) & 0xFF])


def hexs(x) -> str:
    return " ".join(f"{b:02X}" for b in x) if x else "(空)"


def decode_anc(resp: bytes):
    """解析 0xCC 响应,返回 (模式号, db)。"""
    if len(resp) < 7 or resp[0] != 0xBB or resp[1] != APP_CODE or resp[2] != CMD_ANC_QUERY:
        return None, None
    data = resp[5:-1]
    if len(data) < 2:
        return None, None
    mode = data[1] ^ XOR_KEY
    db = (data[2] ^ XOR_KEY) if len(data) > 2 else None
    return mode, db


def mode_name(m):
    for _, (idx, cn) in MODES.items():
        if idx == m:
            return cn
    return f"未知({m})"


class Session:
    def __init__(self):
        self.responses = []
        self.ch_write = None

    async def connect(self, addr: str):
        from winrt.windows.devices.bluetooth import BluetoothLEDevice
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue as CCCD)

        le = None
        for _ in range(8):
            le = await BluetoothLEDevice.from_bluetooth_address_async(
                int(addr.replace(":", ""), 16))
            if le is not None:
                break
            await asyncio.sleep(2)
        if le is None:
            raise RuntimeError(f"找不到设备 {addr}")
        print(f"设备: {le.name}  (is_paired={le.device_information.pairing.is_paired})")

        from winrt.windows.devices.bluetooth import BluetoothCacheMode

        self.svc = None
        svcs = []
        for attempt in range(4):
            mode = BluetoothCacheMode.CACHED if attempt < 2 else BluetoothCacheMode.UNCACHED
            svcs = list((await le.get_gatt_services_with_cache_mode_async(mode)).services)
            self.svc = next((s for s in svcs if str(s.uuid).startswith(SVC)), None)
            if self.svc is not None:
                break
            print(f"  未找到 EDIFIER 服务,换用{ '缓存' if attempt < 2 else '非缓存'}模式重试 {attempt + 1}…")
            await asyncio.sleep(1.5)
        print(f"发现 {len(svcs)} 个服务: " + ", ".join(str(s.uuid)[:8] for s in svcs))
        if self.svc is None:
            raise RuntimeError("未找到 EDIFIER 服务")
        chs = []
        for attempt in range(5):
            chs = list((await self.svc.get_characteristics_async()).characteristics)
            if len(chs) >= 2:
                break
            print(f"  特征枚举为空,重试 {attempt + 1}…")
            await asyncio.sleep(1.5)
        ch_n = next((c for c in chs if str(c.uuid).startswith("48090001")), None)
        self.ch_write = next((c for c in chs if str(c.uuid).startswith("48090002")), None)
        if ch_n is None or self.ch_write is None:
            raise RuntimeError("未找到收发特征")

        # 必须持有引用,否则被 GC 后订阅会失效
        self.le = le
        self.ch_notify = ch_n
        ch_n.add_value_changed(self._on_notify)
        await ch_n.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
        await asyncio.sleep(0.6)
        print("已连接并订阅\n")

    def _on_notify(self, _sender, args):
        raw = bytes(memoryview(args.characteristic_value))
        if raw[:1] == b"\xBB":          # 只收设备响应帧
            self.responses.append(raw)

    async def send(self, pkt: bytes, wait=1.5):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattWriteOption
        from winrt.windows.storage.streams import DataWriter

        self.responses.clear()
        d = DataWriter()
        d.write_bytes(pkt)
        await self.ch_write.write_value_with_option_async(
            d.detach_buffer(), GattWriteOption.WRITE_WITHOUT_RESPONSE)
        await asyncio.sleep(wait)
        return self.responses[-1] if self.responses else None

    async def query(self):
        return await self.send(build(CMD_ANC_QUERY), 1.3)

    async def set_mode(self, index: int):
        return await self.send(build(CMD_ANC_SET, [GROUP_INDEX, index]), 2.0)


async def main_async(args):
    s = Session()
    await s.connect(args.device)

    if args.action == "status":
        r = await s.query()
        if r is None:
            print("未收到响应")
            return 1
        m, db = decode_anc(r)
        print(f"原始响应: {hexs(r)}")
        print(f"当前模式: {mode_name(m)}  (编号 {m})" + (f"  db={db}" if db is not None else ""))
        return 0

    if args.action == "set":
        idx, cn = MODES[args.mode]
        r = await s.set_mode(idx)
        print(f"设置「{cn}」-> {hexs(r) if r else '无响应'}")
        r = await s.query()
        m, _ = decode_anc(r)
        name = mode_name(m)
        print(f"回读确认: {name} (编号 {m})")
        return 0 if m == idx else 1

    if args.action == "cycle":
        for key in ("deep", "comfort", "wide", "wind", "ambient"):
            idx, cn = MODES[key]
            await s.set_mode(idx)
            r = await s.query()
            m, _ = decode_anc(r) if r else (None, None)
            ok = "OK" if m == idx else "FAIL"
            print(f"  {cn:5} -> 回读 {mode_name(m) if m else '(无响应)':8} {ok}")
        return 0

    if args.action == "raw":
        pkt = bytes.fromhex(args.hex.replace(" ", ""))
        r = await s.send(pkt)
        print(f"<<< {hexs(r) if r else '(无响应)'}")
        return 0
    return 1


def main():
    ap = argparse.ArgumentParser(description="EDIFIER W830NB 降噪控制")
    ap.add_argument("-d", "--device", default=BLE_ADDR_DEFAULT)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="查询当前降噪模式")
    p = sub.add_parser("set", help="设置降噪模式")
    p.add_argument("mode", choices=sorted(MODES), help="deep/comfort/wind/ambient/off")
    sub.add_parser("cycle", help="依次切换全部模式并回读验证")
    p = sub.add_parser("raw", help="发送原始十六进制帧")
    p.add_argument("hex")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
