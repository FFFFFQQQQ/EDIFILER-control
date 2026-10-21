#!/usr/bin/env python3
"""EDIFIER W830NB 降噪控制台(WebView 版)。

界面:ui/index.html(HTML/CSS/JS,pywebview + WebView2 承载)
通信:Python 侧 BLE 线程 <-> JS 桥
窗口:无边框;关闭收进托盘,托盘菜单里「退出」才真正结束
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import webview

HERE = os.path.dirname(os.path.abspath(__file__))


def _res(*parts) -> str:
    """资源路径。打包成 exe 后资源被解到 sys._MEIPASS。"""
    base = getattr(sys, "_MEIPASS", HERE)
    return os.path.join(base, *parts)


LOG_PATH = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
    "W830NB-Console", "app.log")


def log(msg: str) -> None:
    """写一行运行日志。排查问题时非常有用的,平时也不吵。"""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 512 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:  # noqa: BLE001
        pass


UI = _res("ui", "index.html")
ICON = _res("ui", "icon.ico")

BLE_ADDR_DEFAULT = "C8:24:70:F4:A8:AC"
APP_TITLE = "W830NB 控制台(非官方)"
SVC_PREFIX = "48098501"
CH_NOTIFY = "48090001"
CH_WRITE = "48090002"

APP_CODE = 0xEC
XOR_KEY = 0xA5
GROUP_INDEX = 3
CMD_ANC_QUERY = 0xCC
CMD_ANC_SET = 0xC1
CMD_BATTERY = 0xD0

MODE_IDS = {1, 2, 3, 4, 5}


class CacheError(RuntimeError):
    """Windows GATT 缓存损坏 —— 只有重启蓝牙射频能恢复。"""


def build(cmd: int, data=None) -> bytes:
    d = [b ^ XOR_KEY for b in (data or [])]
    p = [0xAA, APP_CODE, cmd, (len(d) >> 8) & 0xFF, len(d) & 0xFF] + d
    return bytes(p + [sum(p) & 0xFF])


def hexs(x) -> str:
    return " ".join(f"{b:02X}" for b in x) if x else "(空)"


def decode_anc(resp: bytes):
    if not resp or len(resp) < 7 or resp[0] != 0xBB or resp[1] != APP_CODE or resp[2] != CMD_ANC_QUERY:
        return None, None
    data = resp[5:-1]
    if len(data) < 2:
        return None, None
    return data[1] ^ XOR_KEY, (data[2] ^ XOR_KEY) if len(data) > 2 else None


def decode_battery(resp: bytes):
    """0xD0 回帧: BB EC D0 00 01 <电量^0xA5> <crc>"""
    if not resp or len(resp) < 7 or resp[0] != 0xBB or resp[2] != CMD_BATTERY:
        return None
    data = resp[5:-1]
    if not data:
        return None
    level = data[0] ^ XOR_KEY
    return level if 0 <= level <= 100 else None


# --------------------------------------------------------------------------
class BleWorker(threading.Thread):
    """BLE 通信线程;所有 UI 通知通过 out_q 交给主线程执行。"""

    def __init__(self, out_q: queue.Queue):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.cmd_q: queue.Queue = queue.Queue()
        self.responses = []
        self.le = self.svc = self.ch_notify = self.ch_write = None

    def submit(self, name, **kw):
        self.cmd_q.put((name, kw))

    def _emit(self, kind, *args):
        if os.environ.get("EDIFIER_DEBUG"):
            print(f"[emit] {kind} {args}", flush=True)
        self.out_q.put((kind, args))

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:  # noqa: BLE001
            self._emit("log", f"工作线程异常: {type(exc).__name__} {exc}", "err")

    async def _main(self):
        while True:
            try:
                name, kw = self.cmd_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            try:
                await getattr(self, f"do_{name}")(**kw)
            except Exception as exc:  # noqa: BLE001
                self._emit("log", f"[{name}] {type(exc).__name__}: {exc}", "err")
                self._emit("status", "连接失败", "err")

    @staticmethod
    async def reset_radio():
        """重启蓝牙射频 —— Windows 的 GATT 缓存损坏时唯一有效的恢复手段。"""
        try:
            from winrt.windows.devices.radios import Radio, RadioKind, RadioState
        except Exception:  # noqa: BLE001
            return False
        radios = await Radio.get_radios_async()
        bt = next((r for r in radios if r.kind == RadioKind.BLUETOOTH), None)
        if bt is None:
            return False
        await bt.set_state_async(RadioState.OFF)
        await asyncio.sleep(3.0)
        await bt.set_state_async(RadioState.ON)
        await asyncio.sleep(7.0)
        return True

    async def find_device(self, seconds: float = 6.0):
        """扫描 BLE 广播,按名字或协议服务 UUID 找耳机,返回地址列表。"""
        from winrt.windows.devices.bluetooth.advertisement import (
            BluetoothLEAdvertisementWatcher, BluetoothLEScanningMode)

        found = []

        def on_recv(_watcher, args):
            try:
                adv = args.advertisement
                name = (adv.local_name or "").upper()
                uuids = [str(u).lower() for u in adv.service_uuids]
            except Exception:  # noqa: BLE001
                return
            hit = "EDIFIER" in name or any(u.startswith(SVC_PREFIX) for u in uuids)
            if hit:
                addr = f"{args.bluetooth_address:012X}"
                if addr not in found:
                    found.append(addr)
                    log(f"find_device: hit {addr} name={name!r}")

        watcher = BluetoothLEAdvertisementWatcher()
        watcher.scanning_mode = BluetoothLEScanningMode.ACTIVE
        watcher.add_received(on_recv)
        watcher.start()
        await asyncio.sleep(seconds)
        watcher.stop()
        return found

    async def do_connect(self, addr=None):
        """连接。先试给定地址,失败就自动扫描附近的 EDIFIER 耳机。
        GATT 缓存损坏时不擅自重启蓝牙(那会断掉你的音频),
        只提示用户去点「恢复蓝牙」。"""
        addr = (addr or "").strip()

        # 1) 先按已知地址快速试一次(命中的话 1 秒内就连上)
        if addr:
            try:
                await self._connect_once(addr, tries=2)
                return
            except CacheError as exc:
                self._cache_hint(exc)
                return
            except Exception as exc:  # noqa: BLE001
                log(f"connect {addr}: {type(exc).__name__}: {exc}")
                self._emit("log", f"地址 {addr} 连不上,开始自动搜索", "warn")

        # 2) 自动发现
        self._emit("status", "搜索耳机…", "warn")
        found = await self.find_device()
        if not found:
            self._emit("status", "未找到耳机", "err")
            self._emit("log", "扫描 6 秒没找到 EDIFIER 设备,确认耳机已开机且在范围内", "err")
            return

        self._emit("log", "发现: " + ", ".join(found), "info")
        for target in found:
            try:
                await self._connect_once(target, tries=3)
                return
            except CacheError as exc:
                self._cache_hint(exc)
                return
            except Exception as exc:  # noqa: BLE001
                log(f"connect {target}: {type(exc).__name__}: {exc}")
                self._emit("log", f"{target} 连接失败: {exc}", "err")

        self._emit("status", "连接失败", "err")

    def _cache_hint(self, exc):
        self._emit("log", f"{exc} —— 需要点「恢复蓝牙」(会短暂断开音频)", "warn")
        self._emit("status", "缓存异常,可点「恢复蓝牙」", "warn")
        self._emit("cache_error", True)

    async def do_reset_radio(self, addr=None):
        """用户显式触发:重启蓝牙射频(会短暂断开耳机音频)。"""
        self._emit("status", "正在重启蓝牙…", "warn")
        self._emit("log", "重启蓝牙射频(音频会短暂中断,几秒后自动恢复)", "warn")
        self.le = self.svc = self.ch_notify = self.ch_write = None
        await asyncio.sleep(0.8)
        if not await self.reset_radio():
            self._emit("status", "未找到蓝牙适配器", "err")
            return
        self._emit("log", "蓝牙已重启,重新连接", "info")
        await asyncio.sleep(2.0)
        await self.do_connect(addr or BLE_ADDR_DEFAULT)

    async def _connect_once(self, addr, tries: int = 8):
        from winrt.windows.devices.bluetooth import BluetoothCacheMode, BluetoothLEDevice
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattClientCharacteristicConfigurationDescriptorValue as CCCD)

        self._emit("status", "连接中…", "warn")
        self._emit("connected", False)
        try:
            target = int(str(addr).replace(":", ""), 16)
        except ValueError:
            raise RuntimeError(f"地址格式不对: {addr}") from None

        self.le = None
        for _ in range(tries):
            try:
                self.le = await BluetoothLEDevice.from_bluetooth_address_async(target)
            except Exception:  # noqa: BLE001  重启蓝牙后旧对象会失效
                self.le = None
            if self.le is not None:
                break
            await asyncio.sleep(1.5)
        if self.le is None:
            raise RuntimeError("未找到耳机")
        self._emit("log", f"设备 {self.le.name!r}", "info")

        svcs, self.svc = [], None
        for attempt in range(4):
            mode = BluetoothCacheMode.CACHED if attempt < 2 else BluetoothCacheMode.UNCACHED
            svcs = list((await self.le.get_gatt_services_with_cache_mode_async(mode)).services)
            self.svc = next((s for s in svcs if str(s.uuid).startswith(SVC_PREFIX)), None)
            if self.svc is not None:
                break
            await asyncio.sleep(1.5)
        self._emit("log", "服务: " + ", ".join(str(s.uuid)[:8] for s in svcs), "info")
        if self.svc is None:
            raise CacheError("未找到耳机服务")

        chs = []
        for _ in range(5):
            chs = list((await self.svc.get_characteristics_async()).characteristics)
            if len(chs) >= 2:
                break
            await asyncio.sleep(1.5)
        if len(chs) < 2:
            raise CacheError("GATT 特征枚举为空")

        self.ch_notify = next((c for c in chs if str(c.uuid).startswith(CH_NOTIFY)), None)
        self.ch_write = next((c for c in chs if str(c.uuid).startswith(CH_WRITE)), None)
        if self.ch_notify is None or self.ch_write is None:
            self._emit("status", "未找到收发特征", "err")
            return

        self.ch_notify.add_value_changed(self._on_notify)
        await self.ch_notify.write_client_characteristic_configuration_descriptor_async(CCCD.NOTIFY)
        await asyncio.sleep(0.7)
        self._emit("addr", ":".join(f"{b:02X}" for b in target.to_bytes(6, "big")))
        self._emit("status", "已连接", "ok")
        self._emit("connected", True)
        await self.do_query()
        await self.do_query_battery()
        asyncio.ensure_future(self._battery_poll())

    async def _battery_poll(self):
        """每 5 分钟刷新一次电量。"""
        while self.ch_write is not None:
            await asyncio.sleep(300)
            if self.ch_write is None:
                break
            try:
                await self.do_query_battery()
            except Exception:  # noqa: BLE001
                return

    def _on_notify(self, _s, args):
        raw = bytes(memoryview(args.characteristic_value))
        self._emit("log", f"<<< {hexs(raw)}", "recv")
        if raw[:1] == b"\xBB":
            self.responses.append(raw)
            m, _db = decode_anc(raw)
            if m in MODE_IDS:
                self._emit("mode", m)

    async def _write(self, pkt):
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattWriteOption
        from winrt.windows.storage.streams import DataWriter
        self.responses.clear()
        self._emit("log", f">>> {hexs(pkt)}", "send")
        d = DataWriter()
        d.write_bytes(pkt)
        await self.ch_write.write_value_with_option_async(
            d.detach_buffer(), GattWriteOption.WRITE_WITHOUT_RESPONSE)

    async def do_set_mode(self, index):
        if self.ch_write is None:
            self._emit("status", "未连接", "err")
            return
        await self._write(build(CMD_ANC_SET, [GROUP_INDEX, int(index)]))
        await asyncio.sleep(1.3)
        await self.do_query()

    async def do_query(self):
        if self.ch_write is None:
            return
        await self._write(build(CMD_ANC_QUERY))
        await asyncio.sleep(1.1)
        if self.responses:
            m, db = decode_anc(self.responses[-1])
            if m in MODE_IDS:
                self._emit("mode", m)
                self._emit("log", f"    -> 模式 {m}", "info")
        else:
            self._emit("log", "    -> 无响应", "warn")

    async def do_query_battery(self):
        if self.ch_write is None:
            return
        await self._write(build(CMD_BATTERY))
        await asyncio.sleep(1.4)
        for r in reversed(self.responses):
            lv = decode_battery(r)
            if lv is not None:
                self._emit("battery", lv)
                self._emit("log", f"    -> 电量 {lv}%", "info")
                return
        self._emit("log", "    -> 电量无响应", "warn")

    async def do_raw(self, hexstr):
        if self.ch_write is None:
            return
        try:
            pkt = bytes.fromhex(str(hexstr).replace(" ", ""))
        except ValueError as exc:
            self._emit("log", f"十六进制格式错误: {exc}", "err")
            return
        await self._write(pkt)
        await asyncio.sleep(1.4)


# --------------------------------------------------------------------------
def wave_loop(api: "Api") -> None:
    """采集系统正在播放的声音(WASAPI loopback),把真实波形推给界面。

    每次取 2×1024 帧(约 43 ms),降采样成 128 个代表点后推送。
    默认输出设备切换时自动重开。
    """
    import warnings

    log("wave_loop: start")
    try:
        import numpy as np
        import soundcard as sc
        warnings.filterwarnings("ignore", message=".*discontinuity.*")
    except Exception as exc:  # noqa: BLE001
        log(f"wave_loop: import failed {type(exc).__name__}: {exc}")
        api.wave_state("unavailable", f"{type(exc).__name__}: {exc}")
        return
    log("wave_loop: modules ready")

    SR, BLOCK, BATCH = 48000, 1024, 2
    BANDS = 48
    cur_id = None
    ctx = rec = None
    buf = []
    first = True                      # 第一帧直接落位,不做平滑
    disp = np.zeros(BANDS)            # 每个频段的显示值(快起慢落)
    ref = 1e-6                        # 参考峰值(缓慢回落)
    edges = np.geomspace(40.0, 14000.0, BANDS + 1)
    # 频率倾斜补偿:音乐能量天然低频多、高频少,不补的话曲线右半段会是一条平线
    centers = np.sqrt(edges[:-1] * edges[1:])
    tilt_gain = 10.0 ** (2.0 * np.log2(centers / 200.0) / 20.0)
    freqs = np.fft.rfftfreq(BLOCK * BATCH, 1.0 / SR)

    def close():
        nonlocal ctx, rec
        try:
            if ctx:
                ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        ctx = rec = None

    while True:
        try:
            spk = sc.default_speaker()
            if spk.id != cur_id:
                close()
                buf = []
                first = True
                disp[:] = 0.0
                ref = 1e-6
                mic = sc.get_microphone(id=spk.id, include_loopback=True)
                ctx = mic.recorder(samplerate=SR, channels=2, blocksize=BLOCK)
                rec = ctx.__enter__()
                cur_id = spk.id
                api.wave_state("live")
                log(f"wave_loop: loopback -> {spk.name}")

            buf.append(rec.record(numframes=BLOCK))
            if len(buf) < BATCH:
                continue
            mono = np.concatenate(buf, axis=0).mean(axis=1)
            buf = []
            if len(mono) != BLOCK * BATCH:
                continue

            rms = float(np.sqrt(np.mean(mono ** 2)))

            # ---- 频谱形状:x 轴是频段,画出来就是一条起伏的波 ----
            spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
            idx = np.searchsorted(freqs, edges)
            idx[0] = 0
            idx[-1] = len(spec)
            band = np.array([
                float(spec[a:max(b, a + 1)].max())   # 低频段很窄,至少取一个 bin
                for a, b in zip(idx[:-1], idx[1:])
            ]) * tilt_gain
            # 相对最强频段的 dB 形状:30 dB 之内映射到 0..1,
            # 不直接用线性幅度,否则低频一压,高频全是一条平线
            rel_db = 20.0 * np.log10(band / (band.max() + 1e-12) + 1e-9)
            band = np.clip((rel_db + 30.0) / 30.0, 0.0, 1.0)
            band = np.convolve(band, [0.25, 0.5, 0.25], mode="same")

            # 相对参考峰值(缓慢回落)的 dB 高度:22 dB 之内映射到 0..1,
            # 窗口太宽的话各频段都挤在满格,看起来就是一堵墙
            cur_max = float(band.max()) + 1e-12
            ref = max(cur_max, ref * 0.99)
            rel_db = 20.0 * np.log10(band / ref + 1e-9)
            level = np.clip((rel_db + 22.0) / 22.0, 0.0, 1.0) ** 1.5

            # 起得快、落得慢 —— 频谱条才有"顶上去"的冲击感
            if first:
                disp[:] = level
            else:
                disp += np.where(level > disp, 0.72, 0.16) * (level - disp)

            vals = (np.zeros(BANDS) if rms < 0.0008          # 真静音就干脆躺平
                    else disp)
            first = False
            api.push_wave(",".join(f"{v:.3f}" for v in vals))
        except Exception as exc:  # noqa: BLE001
            log(f"wave_loop: error {type(exc).__name__}: {exc}")
            api.wave_state("unavailable", f"{type(exc).__name__}: {exc}")
            close()
            cur_id = None
            time.sleep(2.0)


# --------------------------------------------------------------------------
class Api:
    """暴露给 JS 的接口。"""

    def __init__(self):
        self.window = None
        self.ready = False              # 页面加载完成后才允许 evaluate_js
        self.worker = BleWorker(queue.Queue())
        self.worker.start()
        self._wave_lock = threading.Lock()
        self.wave_enabled = True

    # ---- 真实波形 ----
    def push_wave(self, csv: str) -> None:
        """把一帧频谱形状推给界面(在采集线程里调用)。"""
        win = self.window
        if win is None or not self.wave_enabled or not self.ready:
            return
        if not self._wave_lock.acquire(blocking=False):
            return                      # 上一帧还没画完,丢掉这帧
        try:
            win.evaluate_js(f"window.__wave&&window.__wave({json.dumps(csv)})")
        except Exception as exc:  # noqa: BLE001
            now = time.time()
            if now - getattr(self, "_wave_err_at", 0.0) > 5.0:
                self._wave_err_at = now
                log(f"push_wave: {type(exc).__name__}: {exc}")
        finally:
            self._wave_lock.release()

    def wave_state(self, state: str, detail: str = "") -> None:
        self.worker.out_q.put(("wave_state", (state, detail)))

    # ---- JS -> Python ----
    def set_mode(self, index):
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return False
        if idx not in MODE_IDS:
            return False
        self.worker.submit("set_mode", index=idx)
        return True

    def query(self):
        self.worker.submit("query")
        return True

    def query_battery(self):
        self.worker.submit("query_battery")
        return True

    def reset_radio(self, addr=None):
        """用户显式点「恢复蓝牙」才调用 —— 会短暂中断耳机音频。"""
        self.worker.submit("reset_radio", addr=addr or BLE_ADDR_DEFAULT)
        return True

    def raw(self, hexstr):
        self.worker.submit("raw", hexstr=hexstr)
        return True

    def connect(self, addr=None):
        # 地址留空 -> 自动扫描
        self.worker.submit("connect", addr=(addr or "").strip())
        return True

    def minimize(self):
        if self.window:
            self.window.minimize()
        return True

    def hide_to_tray(self):
        if self.window:
            self.window.hide()
        return True

    # ---- Python -> JS(在 UI 线程执行)----
    def pump(self):
        """由定时线程驱动,把 BLE 线程的消息转成 JS 调用。"""
        if not self.ready:
            return                      # 页面没加载完,消息先留在队列里
        q = self.worker.out_q
        pending = []
        while True:
            try:
                kind, args = q.get_nowait()
            except queue.Empty:
                break
            pending.append((kind, args))
        if not pending:
            return
        script = []
        for kind, args in pending:
            if kind == "log":
                script.append(f"addLog({json.dumps(args[0])},{json.dumps(args[1])})")
            elif kind == "status":
                script.append(f"setStatus({json.dumps(args[0])},{json.dumps(args[1])})")
            elif kind == "connected":
                script.append(f"setConnected({str(bool(args[0])).lower()})")
            elif kind == "mode":
                script.append(f"applyMode({int(args[0])})")
            elif kind == "battery":
                script.append(f"setBattery({int(args[0])})")
            elif kind == "addr":
                script.append(f"window.setAddr&&window.setAddr({json.dumps(args[0])})")
            elif kind == "wave_state":
                script.append(
                    f"window.waveState&&window.waveState({json.dumps(args[0])},"
                    f"{json.dumps(args[1] if len(args) > 1 else '')})")
                if len(args) > 1 and args[1]:
                    script.append(f"addLog({json.dumps('波形: ' + str(args[1]))},\"warn\")")
        if script and self.window is not None:
            try:
                self.window.evaluate_js(";".join(script))
                if os.environ.get("EDIFIER_DEBUG"):
                    print(f"[js] {len(script)} cmd ok", flush=True)
            except Exception as exc:  # noqa: BLE001
                if os.environ.get("EDIFIER_DEBUG"):
                    print(f"[js] 失败: {type(exc).__name__}: {exc}", flush=True)

    def pump_loop(self):
        while True:
            try:
                self.pump()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.12)


# --------------------------------------------------------------------------
def _find_hwnd(title: str):
    """按标题 + 本进程 PID 找到顶层窗口句柄。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hits = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value == title:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == os.getpid():
                    hits.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else None


def apply_window_icon(title: str, ico_path: str, tries: int = 40) -> None:
    """pywebview 的 EdgeChromium 后端不认 start(icon=...),
    只能等窗口出现后用 WM_SETICON 自己贴。"""
    import ctypes

    if not os.path.exists(ico_path):
        return
    user32 = ctypes.windll.user32
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1

    big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 0, 0,
                            LR_LOADFROMFILE | LR_DEFAULTSIZE)
    small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)

    for _ in range(tries):
        hwnd = _find_hwnd(title)
        if hwnd:
            # 反复设几次:WebView2 初始化完成后可能把图标顶掉
            for _ in range(3):
                if big:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
                if small:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
                time.sleep(0.6)
            return
        time.sleep(0.25)


def build_tray(api: Api):
    try:
        import pystray
        import PIL.Image
    except Exception:  # noqa: BLE001
        return None

    try:
        icon_img = PIL.Image.open(_res("ui", "icon.png")).convert("RGBA")
    except Exception:  # noqa: BLE001
        icon_img = PIL.Image.new("RGBA", (256, 256), (11, 13, 16, 255))

    def show(*_):
        if api.window:
            api.window.show()

    def quit_(*_):
        icon.stop()
        if api.window:
            api.window.destroy()

    icon = pystray.Icon(
        "edifier", icon_img, APP_TITLE,
        pystray.Menu(
            pystray.MenuItem("显示窗口", show, default=True),
            pystray.MenuItem("查询状态", lambda *_: api.query()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", quit_),
        ))
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def _set_aumid(app_id: str = "Edifier.W830NB.Console") -> None:
    """给进程一个独立 AppUserModelID。否则任务栏按 pythonw.exe 分组,
    会一直显示 Python 的 exe 图标,无视 WM_SETICON。"""
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:  # noqa: BLE001
        pass


def main():
    _set_aumid()
    log(f"=== start frozen={getattr(sys, 'frozen', False)} exe={sys.executable}")
    api = Api()
    win = webview.create_window(
        APP_TITLE,
        url=UI,
        js_api=api,
        width=720, height=660,
        frameless=True,
        easy_drag=True,
        resizable=False,
        background_color="#08090B",
        min_size=(720, 660),
    )
    api.window = win
    threading.Thread(target=api.pump_loop, daemon=True).start()
    threading.Thread(target=wave_loop, args=(api,), daemon=True).start()
    threading.Thread(target=apply_window_icon,
                     args=(APP_TITLE, ICON), daemon=True).start()
    build_tray(api)

    def on_loaded():
        api.ready = True
        log("page loaded")
        print("[main] 页面已加载,开始自动连接", flush=True)
        api.connect(BLE_ADDR_DEFAULT)

    win.events.loaded += on_loaded
    webview.start(debug=False, icon=ICON if os.path.exists(ICON) else None)


if __name__ == "__main__":
    main()
