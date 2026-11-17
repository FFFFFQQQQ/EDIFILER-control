#!/usr/bin/env python3
"""锁屏时暂停正在播放的媒体。

两件事:
  1. 锁屏检测 —— WTSRegisterSessionNotification + 一个隐藏窗口的消息循环
     (事件驱动,不轮询)
  2. 暂停播放 —— Windows SMTC(GlobalSystemMediaTransportControlsSessionManager)
     只暂停 playback_status == PLAYING 的会话,本来暂停的不动
解锁后不自动恢复。
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import threading
from ctypes import wintypes

WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
NOTIFY_FOR_THIS_SESSION = 0

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


_user32.RegisterClassW.restype = ctypes.c_ushort
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.DefWindowProcW.restype = LRESULT
_user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]


class SessionLockWatcher(threading.Thread):
    """隐藏窗口 + WTS 通知,锁屏/解锁时回调。daemon 线程。"""

    def __init__(self, on_lock=None, on_unlock=None, log=None):
        super().__init__(daemon=True, name="SessionLockWatcher")
        self.on_lock = on_lock
        self.on_unlock = on_unlock
        self.log = log or (lambda _msg: None)
        self.error: BaseException | None = None
        self._ready = threading.Event()
        self._hwnd = None
        self._proc = None

    def start(self):
        super().start()
        if not self._ready.wait(5.0):
            raise RuntimeError("锁屏监听初始化超时")
        if self.error is not None:
            raise RuntimeError(f"锁屏监听初始化失败: {self.error}") from self.error

    def run(self):
        try:
            self._init_window()
        except Exception as exc:  # noqa: BLE001
            self.error = exc
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _init_window(self):
        hinst = _kernel32.GetModuleHandleW(None)
        cls = f"W830NB.LockWatch.{os.getpid()}.{id(self)}"
        self._proc = WNDPROC(self._wndproc)     # 必须持引用,否则回调被回收
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = cls
        if not _user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:                     # 1410 = 类已注册,可忽略
                raise ctypes.WinError(err)
        self._hwnd = _user32.CreateWindowExW(
            0, cls, None, 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        if not _wtsapi32.WTSRegisterSessionNotification(
                self._hwnd, NOTIFY_FOR_THIS_SESSION):
            raise ctypes.WinError(ctypes.get_last_error())

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_WTSSESSION_CHANGE:
                if wparam == WTS_SESSION_LOCK and self.on_lock:
                    self.on_lock()
                elif wparam == WTS_SESSION_UNLOCK and self.on_unlock:
                    self.on_unlock()
        except Exception as exc:  # noqa: BLE001
            self.log(f"锁屏回调异常: {type(exc).__name__}: {exc}")
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def post_for_test(self, wparam: int) -> None:
        """测试用:给自己的窗口塞一条假的 WTS 消息。"""
        _user32.PostMessageW(self._hwnd, WM_WTSSESSION_CHANGE, wparam, 0)


async def _pause_playing_async() -> list[str]:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
    )

    mgr = await Manager.request_async()
    paused = []
    for session in mgr.get_sessions():
        try:
            if session.get_playback_info().playback_status != Status.PLAYING:
                continue
            if await session.try_pause_async():
                paused.append(session.source_app_user_model_id)
        except Exception:  # noqa: BLE001  单个播放器失败不影响其它
            continue
    return paused


def pause_playing_media(timeout: float = 3.0) -> list[str]:
    """暂停所有正在播放的媒体会话,返回被暂停的 app id 列表。"""

    async def run():
        return await asyncio.wait_for(_pause_playing_async(), timeout)

    return asyncio.run(run())
