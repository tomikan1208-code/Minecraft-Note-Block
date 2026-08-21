"""検証用の Minecraft 専用サーバ（headless）。

生成したデータパックが**実際に読み込めてコマンドが通るか**を、ゲームを起動せずに
確かめる。サーバは音を鳴らさないので音そのものの検証はできないが、

* データパックの読み込みエラー
* コマンドの構文エラー
* 音符ブロックが本当に設置されたか（``/execute if block`` で問い合わせる）

はここで全部わかる。あとで v1 の測定リグ（RCON でサーバを操作しつつ
クライアントの音をループバック録音する）の土台にもなる。

    uv run python -m mcnb.server --verify out/tests/01_single/01_single_datapack

サーバ jar は Mojang 公式の配布元から、ローカルのバージョン JSON に書かれている
URL と SHA1 を使って取得する（ハッシュが合わなければ捨てる）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from queue import Empty, Queue

from .mcassets import AssetError, default_minecraft_dir, resolve_version

#: Minecraft 26.x が要求する Java のメジャーバージョン
REQUIRED_JAVA = 21
RCON_PORT = 25575
SERVER_PORT = 25566
RCON_PASSWORD = "mcnb-local"


class ServerError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Java
# --------------------------------------------------------------------------- #


def _java_major(java: Path) -> int | None:
    try:
        proc = subprocess.run(
            [str(java), "-version"], capture_output=True, text=True, timeout=30, errors="replace"
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stderr or "") + (proc.stdout or "")
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    # 1.8.0_x のような旧表記
    if major == 1 and m.group(2):
        return int(m.group(2))
    return major


def find_java(minimum: int = REQUIRED_JAVA) -> Path:
    """``minimum`` 以上の java を探す。``MCNB_JAVA`` で明示指定できる。"""
    candidates: list[Path] = []

    env = os.environ.get("MCNB_JAVA")
    if env:
        candidates.append(Path(env))

    if platform.system() == "Windows":
        for base in (Path(r"C:\Program Files\Java"), Path(r"C:\Program Files\Eclipse Adoptium"),
                     Path(r"C:\Program Files\Microsoft")):
            if base.is_dir():
                candidates.extend(sorted(base.glob("*/bin/java.exe"), reverse=True))
        # ランチャー同梱のランタイム
        runtime = default_minecraft_dir() / "runtime"
        if runtime.is_dir():
            candidates.extend(sorted(runtime.glob("**/bin/java.exe"), reverse=True))
    else:
        which = shutil.which("java")
        if which:
            candidates.append(Path(which))
        for base in (Path("/usr/lib/jvm"), Path("/Library/Java/JavaVirtualMachines")):
            if base.is_dir():
                candidates.extend(sorted(base.glob("*/bin/java"), reverse=True))
                candidates.extend(sorted(base.glob("*/Contents/Home/bin/java"), reverse=True))

    seen: set[Path] = set()
    for c in candidates:
        if c in seen or not c.is_file():
            continue
        seen.add(c)
        major = _java_major(c)
        if major and major >= minimum:
            return c

    raise ServerError(
        f"Java {minimum}+ が見つかりません。MCNB_JAVA=<java の場所> で指定してください。"
    )


# --------------------------------------------------------------------------- #
# サーバ jar
# --------------------------------------------------------------------------- #


def server_download_info(mc_version: str, launcher_dir: Path) -> tuple[str, str, int]:
    """ローカルのバージョン JSON から ``(url, sha1, size)`` を取り出す。"""
    meta = launcher_dir / "versions" / mc_version / f"{mc_version}.json"
    if not meta.is_file():
        raise ServerError(f"{meta} がありません。ランチャーで {mc_version} を一度起動してください。")
    data = json.loads(meta.read_text(encoding="utf-8"))
    server = data.get("downloads", {}).get("server")
    if not server:
        raise ServerError(f"{mc_version} にサーバ jar の配布情報がありません。")
    return server["url"], server["sha1"], server["size"]


def ensure_server_jar(root: Path, mc_version: str, launcher_dir: Path) -> Path:
    """サーバ jar を用意する。既にあって SHA1 が一致すれば何もしない。"""
    url, sha1, size = server_download_info(mc_version, launcher_dir)
    root.mkdir(parents=True, exist_ok=True)
    jar = root / f"minecraft_server.{mc_version}.jar"

    if jar.is_file() and hashlib.sha1(jar.read_bytes()).hexdigest() == sha1:
        return jar

    print(f"  サーバ jar を取得: {url}")
    print(f"    {size / 1048576:.1f} MB  sha1={sha1}")
    with urllib.request.urlopen(url, timeout=300) as r:
        blob = r.read()
    got = hashlib.sha1(blob).hexdigest()
    if got != sha1:
        raise ServerError(f"SHA1 不一致（期待 {sha1} / 実際 {got}）。破棄しました。")
    jar.write_bytes(blob)
    return jar


# --------------------------------------------------------------------------- #
# RCON（最小実装）
# --------------------------------------------------------------------------- #

_RCON_AUTH = 3
_RCON_EXEC = 2
_RCON_RESPONSE = 0
#: Minecraft の RCON が応答を分割する単位
RESPONSE_CHUNK = 4096


class Rcon:
    """Source RCON の最小クライアント。依存を増やさないため自前で持つ。"""

    def __init__(self, host: str = "127.0.0.1", port: int = RCON_PORT, password: str = RCON_PASSWORD):
        self.host, self.port, self.password = host, port, password
        self.sock: socket.socket | None = None
        self._id = 0

    def __enter__(self) -> Rcon:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        response_id, _ = self._roundtrip(_RCON_AUTH, self.password)
        if response_id == -1:
            raise ServerError("RCON の認証に失敗しました。")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def _send(self, packet_type: int, body: str) -> int:
        assert self.sock is not None
        self._id += 1
        payload = struct.pack("<ii", self._id, packet_type) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        return self._id

    def _recv(self) -> tuple[int, str]:
        assert self.sock is not None

        def read(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    raise ServerError("RCON の接続が切れました。")
                buf += chunk
            return buf

        (length,) = struct.unpack("<i", read(4))
        payload = read(length)
        response_id, _packet_type = struct.unpack("<ii", payload[:8])
        return response_id, payload[8:-2].decode("utf-8", errors="replace")

    def _roundtrip(self, packet_type: int, body: str) -> tuple[int, str]:
        self._send(packet_type, body)
        return self._recv()

    def command(self, text: str) -> str:
        """コマンドを1つ実行して応答を返す。

        Minecraft の RCON は長い応答を ``RESPONSE_CHUNK`` バイトごとに分割して送る。
        1パケットしか読まないと**次のコマンドが前の応答を受け取ってしまう**ので、
        上限ちょうどのパケットが来ている間は読み継ぐ。

        番兵パケットを別に送る手も使えない — Minecraft の RCON は1回の read で
        1パケットしか処理せず、続けて送ると接続ごと切られる。
        """
        self._send(_RCON_EXEC, text)
        chunks: list[str] = []
        while True:
            _id, chunk = self._recv()
            chunks.append(chunk)
            if len(chunk.encode("utf-8")) < RESPONSE_CHUNK:
                break
        return "".join(chunks)


# --------------------------------------------------------------------------- #
# サーバ本体
# --------------------------------------------------------------------------- #

DONE_RE = re.compile(r'Done \([\d.]+s\)! For help')
ERROR_RE = re.compile(r"(ERROR|FATAL|Exception|Failed to|Couldn't|Unknown|Whitelist)", re.I)


class Server:
    """headless の専用サーバを起こして RCON で叩く。"""

    def __init__(self, root: Path, jar: Path, java: Path, memory: str = "2G"):
        # サーバは cwd=root で起動するので、jar と java は絶対パスにしておく
        self.root = Path(root).resolve()
        self.jar = Path(jar).resolve()
        self.java = Path(java).resolve()
        self.memory = memory
        self.proc: subprocess.Popen | None = None
        self.log: list[str] = []
        self._queue: Queue[str] = Queue()
        self._reader: threading.Thread | None = None

    # -- 設定 ------------------------------------------------------------- #

    def configure(self, accept_eula: bool, level_name: str = "world", extra: dict | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

        # EULA。ユーザーの明示的な許可がある場合のみ true にする
        (self.root / "eula.txt").write_text(
            "# https://aka.ms/MinecraftEULA\n"
            f"eula={'true' if accept_eula else 'false'}\n",
            encoding="utf-8",
        )

        props = {
            "level-name": level_name,
            # 地形は呼び出し側が決める。generator-settings を書かなければ
            # バニラのデフォルト（岩盤+土2+草）のフラットになる
            "level-type": "minecraft:flat",
            "gamemode": "creative",
            "force-gamemode": "true",
            "difficulty": "peaceful",
            "spawn-monsters": "false",
            "spawn-npcs": "false",
            "spawn-animals": "false",
            "online-mode": "false",       # ローカル検証専用
            "server-port": str(SERVER_PORT),
            "enable-rcon": "true",
            "rcon.port": str(RCON_PORT),
            "rcon.password": RCON_PASSWORD,
            "broadcast-rcon-to-ops": "false",
            "max-tick-time": "-1",         # 検証中に重い setblock を打つので watchdog を切る
            "view-distance": "12",
            "simulation-distance": "12",
            "sync-chunk-writes": "false",
            "enable-command-block": "true",
            "op-permission-level": "4",
            "function-permission-level": "4",
            "white-list": "false",
            "motd": "mcnb verification server",
        }
        props.update(extra or {})
        (self.root / "server.properties").write_text(
            "\n".join(f"{k}={v}" for k, v in props.items()) + "\n", encoding="utf-8"
        )

    def install_datapack(self, pack: Path, level_name: str = "world") -> Path:
        dest = self.root / level_name / "datapacks" / pack.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(pack, dest)
        return dest

    # -- 起動 / 停止 ------------------------------------------------------ #

    def start(self, timeout: float = 300.0) -> None:
        args = [
            str(self.java),
            f"-Xms{self.memory}", f"-Xmx{self.memory}",
            "-XX:+UseG1GC",
            "-Dcom.mojang.eula.agree=true",
            "-jar", str(self.jar),
            "--nogui",
        ]
        self.proc = subprocess.Popen(
            args,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

        def pump() -> None:
            assert self.proc and self.proc.stdout
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                self.log.append(line)
                self._queue.put(line)

        self._reader = threading.Thread(target=pump, daemon=True)
        self._reader.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise ServerError(
                    "サーバが起動前に終了しました:\n" + "\n".join(self.log[-25:])
                )
            try:
                line = self._queue.get(timeout=1.0)
            except Empty:
                continue
            if DONE_RE.search(line):
                return
        raise ServerError("サーバの起動がタイムアウトしました:\n" + "\n".join(self.log[-25:]))

    def stop(self, timeout: float = 90.0) -> None:
        if not self.proc:
            return
        try:
            with Rcon() as rcon:
                rcon.command("stop")
        except (OSError, ServerError):
            if self.proc.stdin:
                try:
                    self.proc.stdin.write("stop\n")
                    self.proc.stdin.flush()
                except OSError:
                    pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    # -- ログ ------------------------------------------------------------- #

    def drain(self) -> list[str]:
        """前回の drain 以降に出たログ行。"""
        lines = []
        while True:
            try:
                lines.append(self._queue.get_nowait())
            except Empty:
                break
        return lines

    def errors(self) -> list[str]:
        return [line for line in self.log if ERROR_RE.search(line)]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2] / ".minecraft" / "server"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="検証用の Minecraft 専用サーバ")
    ap.add_argument("--root", type=Path, default=None, help="既定: <repo>/.minecraft/server")
    ap.add_argument("--mc", default=None, help="既定: インストール済みの最新リリース")
    ap.add_argument("--memory", default="2G")
    ap.add_argument("--accept-eula", action="store_true",
                    help="Minecraft EULA に同意する（https://aka.ms/MinecraftEULA）")
    ap.add_argument("--setup", action="store_true", help="jar と設定を用意するだけ")
    args = ap.parse_args(argv)

    try:
        launcher = default_minecraft_dir()
        mc = args.mc or resolve_version(launcher)
        root = args.root or _default_root()

        java = find_java()
        print(f"Java  : {java} (major {_java_major(java)})")
        jar = ensure_server_jar(root, mc, launcher)
        print(f"jar   : {jar.name}")

        srv = Server(root, jar, java, memory=args.memory)
        srv.configure(accept_eula=args.accept_eula)
        print(f"設定  : {root / 'server.properties'}")
        if not args.accept_eula:
            print("\nEULA に未同意です。--accept-eula を付けるか eula.txt を書き換えてください。")
            return 0 if args.setup else 1
        if args.setup:
            return 0

        print("\n起動中…")
        srv.start()
        print("起動しました。RCON で /list を実行:")
        with Rcon() as rcon:
            print("  " + rcon.command("list"))
        srv.stop()
        print("停止しました。")

    except (ServerError, AssetError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
