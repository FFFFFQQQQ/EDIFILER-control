#!/usr/bin/env python3
"""每日定时关机:连接 W830NB,发送 0xCE,让耳机断电。

由 Windows 任务计划程序调用(pythonw.exe,无控制台窗口)。
日志: %LOCALAPPDATA%\\W830NB-Console\\poweroff_task.log
退出码: 0 收到应答;1 无应答或异常(耳机没开/不在范围内/被手机占用)。
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from edifier_anc import BLE_ADDR_DEFAULT, CMD_POWER_OFF, Session, build, hexs

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "W830NB-Console"
LOG_PATH = LOG_DIR / "poweroff_task.log"


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)
    if sys.stdout is not None:
        print(line, end="", flush=True)


async def run() -> int:
    s = Session()
    await s.connect(BLE_ADDR_DEFAULT)
    ack = await s.send(build(CMD_POWER_OFF), 2.0)
    if ack is None:
        log("关机命令无应答")
        return 1
    log(f"关机命令已确认: {hexs(ack)}")
    return 0


def main() -> int:
    if sys.stdout is None:              # pythonw.exe 下没有控制台
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        log(f"失败: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
