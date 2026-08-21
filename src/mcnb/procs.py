"""前回の残骸を掃除してから立ち上げる。

GUI のポートが埋まっていたり、検証・測定で立てた Minecraft サーバが
残っていたりすると、次の起動が失敗する。

**巻き添えを出さないこと**を最優先にしている:

* ポートは**指定したポートを実際に LISTEN しているプロセス**だけ
* Java は**このリポジトリのサーバ jar を動かしているもの**だけ
  （あなたが遊んでいる Minecraft クライアントや、他の Java アプリは触らない）
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass

#: mcnb が使うポート
GUI_PORT = 8770
SERVER_PORT = 25566
RCON_PORT = 25575


@dataclass(frozen=True)
class Process:
    pid: int
    what: str

    def __str__(self) -> str:
        return f"pid {self.pid} ({self.what})"


def _run(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def listeners_on(port: int) -> list[Process]:
    """そのポートを LISTEN しているプロセス。"""
    if sys.platform != "win32":
        out = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"])
        return [Process(int(p), f"port {port}") for p in out.split() if p.isdigit()]

    found: set[int] = set()
    for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        # ローカルアドレスは 0.0.0.0:8770 や [::]:8770 の形
        local = parts[1]
        if not local.endswith(f":{port}"):
            continue
        if parts[-1].isdigit():
            found.add(int(parts[-1]))
    return [Process(pid, f"port {port}") for pid in sorted(found)]


def stale_java(marker: str = "minecraft_server") -> list[Process]:
    """``marker`` を含むコマンドラインで動いている java。

    あなたが遊んでいる Minecraft クライアントは jar 名が違うので当たらない。
    """
    if sys.platform != "win32":
        out = _run(["pgrep", "-f", marker])
        return [Process(int(p), "minecraft server") for p in out.split() if p.isdigit()]

    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{marker}*' }} | "
        "Select-Object -ExpandProperty ProcessId"
    )
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])
    return [
        Process(int(m), "minecraft server")
        for m in re.findall(r"^\s*(\d+)\s*$", out, re.M)
    ]


def kill(process: Process) -> bool:
    """止める。自分自身は絶対に止めない。"""
    if process.pid == os.getpid():
        return False
    if sys.platform == "win32":
        result = _run(["taskkill", "/F", "/T", "/PID", str(process.pid)])
        return "SUCCESS" in result.upper() or "成功" in result
    return bool(_run(["kill", "-9", str(process.pid)]) is not None)


def cleanup(ports: list[int] | None = None, java: bool = True, verbose: bool = True) -> int:
    """残骸を掃除する。止めた数を返す。"""
    targets: list[Process] = []
    for port in ports or []:
        targets.extend(listeners_on(port))
    if java:
        targets.extend(stale_java())

    # 同じ PID を二度止めない
    seen: set[int] = set()
    stopped = 0
    for process in targets:
        if process.pid in seen or process.pid == os.getpid():
            continue
        seen.add(process.pid)
        if verbose:
            print(f"  前回の残骸を止めます: {process}")
        if kill(process):
            stopped += 1
    return stopped


def main(argv: list[str] | None = None) -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="mcnb が残したプロセスを止める")
    ap.add_argument("--ports", type=int, nargs="*",
                    default=[GUI_PORT, SERVER_PORT, RCON_PORT])
    ap.add_argument("--no-java", action="store_true", help="Minecraft サーバは止めない")
    args = ap.parse_args(argv)

    print("■ 前回の残骸を片付けています")
    stopped = cleanup(ports=args.ports, java=not args.no_java)
    print("  残骸はありませんでした" if stopped == 0 else f"  {stopped} 個止めました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
