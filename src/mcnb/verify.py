"""生成したデータパックを headless サーバで実際に動かして検証する。

「理論上動くコード」で止めないための仕組み。ゲームを起動せずに、

1. データパックが読み込めるか
2. ``mcnb:build`` が音符ブロックを本当に置けたか（ブロックステートまで一致するか）
3. ``/tick freeze`` + ``/tick step`` で 1 tick ずつ進めて、
   発火用レッドストーンが**正しい tick に正しい場所へ現れて消えるか**
4. サーバログにエラーが出ていないか

を確かめる。音そのものは鳴らないので、音の良し悪しは別（v1 の測定リグ）。

    uv run mcnb verify tests/inputs/01_single.mid
"""

from __future__ import annotations

import json
import random
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .instruments import INSTRUMENTS, block_state
from .layout import Layout
from .mcassets import default_minecraft_dir, resolve_version
from .server import Rcon, Server, ServerError, ensure_server_jar, find_java

LEVEL_NAME = "world"
#: build は 1 区画 1 tick でチェーンするので、余裕をみて待つ
BUILD_TIMEOUT = 180.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class VerifyResult:
    checks: list[Check] = field(default_factory=list)
    log_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks) and not self.log_errors

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))


#: チャンクが読み込まれていないと `execute if block` はこれを返す。
#: 「ブロックが違う」と区別できないと、検証が黙って嘘をつく。
NOT_LOADED = "not loaded"
#: forceload してからチャンクが実際に載るまでの待ち（秒）
LOAD_WAIT = 2.0
#: 一度に forceload する X 幅（ブロック）。チャンク上限に当てないため
FORCELOAD_WINDOW = 256
#: /tick step の完了待ちのポーリング間隔（秒）
STEP_POLL = 0.1


class NotLoaded(RuntimeError):
    pass


def _block_at(rcon: Rcon, pos: tuple[int, int, int], predicate: str) -> bool:
    x, y, z = pos
    response = rcon.command(f"execute if block {x} {y} {z} {predicate}")
    if NOT_LOADED in response:
        raise NotLoaded(f"({x}, {y}, {z}) が読み込まれていません")
    return "Test passed" in response


def _score(rcon: Rcon, holder: str, objective: str = "mcnb") -> int | None:
    response = rcon.command(f"scoreboard players get {holder} {objective}")
    m = re.search(r"has (-?\d+)", response)
    return int(m.group(1)) if m else None


def _wait_for_tick(rcon: Rcon, expected: int, steps: int) -> bool:
    """``#t`` が ``expected`` に達するまで待つ。達しなければ False。"""
    deadline = time.time() + steps / 20.0 + 5.0
    while time.time() < deadline:
        if (_score(rcon, "#t") or 0) >= expected:
            return True
        time.sleep(STEP_POLL)
    return False


def _forceload(rcon: Rcon, x1: int, z1: int, x2: int, z2: int) -> None:
    rcon.command("forceload remove all")
    rcon.command(f"forceload add {x1} {z1} {x2} {z2}")
    time.sleep(LOAD_WAIT)


def verify_layout(
    layout: Layout,
    pack_dir: Path,
    root: Path | None = None,
    mc: str | None = None,
    sample: int = 24,
    keep_world: bool = False,
    memory: str = "2G",
) -> VerifyResult:
    """データパックをサーバで動かして検証する。"""
    launcher = default_minecraft_dir()
    mc = mc or resolve_version(launcher)
    root = root or Path(__file__).resolve().parents[2] / ".minecraft" / "server"

    java = find_java()
    jar = ensure_server_jar(root, mc, launcher)

    # 毎回まっさらなワールドから始める。前回の残骸で通ってしまうのを防ぐ
    world = root / LEVEL_NAME
    if world.exists() and not keep_world:
        shutil.rmtree(world)

    srv = Server(root, jar, java, memory=memory)
    # 検証は地形が要らないのでボイドにして軽くする
    srv.configure(
        accept_eula=True,
        level_name=LEVEL_NAME,
        extra={
            "level-type": "minecraft:flat",
            "generator-settings": json.dumps(
                {"layers": [{"block": "minecraft:air", "height": 1}], "biome": "minecraft:the_void"}
            ),
        },
    )
    srv.install_datapack(pack_dir, level_name=LEVEL_NAME)

    result = VerifyResult()
    (_bx1, _by1, bz1), (_bx2, _by2, bz2) = layout.bounds()
    rng = random.Random(0)
    placements = layout.placements
    picks = placements if len(placements) <= sample else rng.sample(placements, sample)

    srv.start()
    try:
        with Rcon() as rcon:
            # --- 1. データパックが有効か --------------------------------- #
            listing = rcon.command("datapack list enabled")
            result.add(
                "データパックが有効",
                pack_dir.name in listing,
                listing.replace("\n", " ")[:160],
            )

            # --- 2. 設置 ------------------------------------------------- #
            rcon.command("function mcnb:setup")
            # ブロックを直接見に行くのではなく、設置完了フラグを待つ。
            # 未読み込みチャンクを覗くと「ブロックが無い」と区別がつかない
            rcon.command("function mcnb:build")

            deadline = time.time() + BUILD_TIMEOUT
            built = False
            while time.time() < deadline:
                time.sleep(1.0)
                if _score(rcon, "#built") == 1:
                    built = True
                    break
            result.add("mcnb:build が完走", built, "#built フラグが立たなかった")

            # --- 3. 置かれたブロックを確認 ------------------------------- #
            # チャンクは非同期に読み込まれるので、確認したい範囲を forceload して待つ
            missing_note, missing_inst, missing_air = [], [], []
            not_loaded: list[str] = []
            picks_sorted = sorted(picks, key=lambda p: p.x)
            window_start = 0
            while window_start < len(picks_sorted):
                base_x = picks_sorted[window_start].x
                window = [p for p in picks_sorted[window_start:] if p.x - base_x < FORCELOAD_WINDOW]
                window_start += len(window)
                _forceload(rcon, base_x - 2, bz1 - 1, window[-1].x + 2, bz2 + 1)

                for p in window:
                    try:
                        if not _block_at(rcon, (p.x, p.y, p.z), block_state(p.instrument, p.key)):
                            missing_note.append(p)
                        block = INSTRUMENTS[p.instrument].block
                        if not _block_at(rcon, p.instrument_block, f"minecraft:{block}"):
                            missing_inst.append(p)
                        if not _block_at(rcon, (p.x, p.y + 1, p.z), "minecraft:air"):
                            missing_air.append(p)
                    except NotLoaded as e:
                        not_loaded.append(str(e))

            result.add(
                f"音符ブロックの音程と楽器 ({len(picks)} 箇所)",
                not missing_note,
                "" if not missing_note else f"不一致 {len(missing_note)}: {missing_note[0]}",
            )
            result.add(
                f"直下の楽器ブロック ({len(picks)} 箇所)",
                not missing_inst,
                "" if not missing_inst else f"不一致 {len(missing_inst)}: {missing_inst[0]}",
            )
            result.add(
                f"真上が空気 ({len(picks)} 箇所)",
                not missing_air,
                "" if not missing_air else f"塞がっている {len(missing_air)}",
            )

            # --- 4. tick を 1 つずつ進めて発火を追う ---------------------- #
            by_tick = layout.placements_by_tick()
            check_ticks = [t for t in sorted(by_tick) if by_tick[t]][:6]

            if check_ticks:
                # player_x は小数になったので forceload に渡す前に整数化する
                last_x = int(layout.player_x(check_ticks[-1])) + 4
                _forceload(rcon, layout.origin[0] - 4, bz1 - 1, last_x, bz2 + 1)

            rcon.command("tick freeze")
            rcon.command("scoreboard players set #t mcnb 0")
            rcon.command("scoreboard players set #playing mcnb 1")

            fired: list[str] = []
            leftover: list[str] = []
            stalled: list[str] = []
            current = 0
            for target in check_ticks:
                steps = target - current + 1
                rcon.command(f"tick step {steps}")
                # /tick step は応答が返った時点ではまだ進んでいない。しかも N tick ぶんの
                # 実時間がかかるので、固定待ちではなくカウンタが追いつくまで待つ
                if not _wait_for_tick(rcon, target + 1, steps):
                    stalled.append(f"tick {target} まで進まなかった")
                current = target + 1

                try:
                    for p in by_tick[target]:
                        if not _block_at(rcon, p.trigger_pos, "minecraft:redstone_block"):
                            fired.append(f"tick {target} {p.trigger_pos}")
                    # 前 tick のぶんが消えているか
                    prev = by_tick.get(target - 1)
                    if prev:
                        for p in prev:
                            if _block_at(rcon, p.trigger_pos, "minecraft:redstone_block"):
                                leftover.append(f"tick {target - 1} {p.trigger_pos}")
                except NotLoaded as e:
                    not_loaded.append(str(e))

            rcon.command("scoreboard players set #playing mcnb 0")
            rcon.command("tick unfreeze")

            result.add(
                f"発火用ブロックが正しい tick に出る ({len(check_ticks)} tick)",
                not fired,
                "" if not fired else f"出ていない: {fired[:3]}",
            )
            result.add(
                "前 tick の発火用ブロックが消える",
                not leftover,
                "" if not leftover else f"残っている: {leftover[:3]}",
            )
            result.add(
                "tick が想定どおり進む",
                not stalled,
                "" if not stalled else f"{stalled[:3]}",
            )
            result.add(
                "確認したい範囲が読み込めた",
                not not_loaded,
                "" if not not_loaded else f"{len(not_loaded)} 箇所: {not_loaded[0]}",
            )
    finally:
        srv.stop()

    # --- 4. ログ ------------------------------------------------------- #
    noise = ("Whitelist", "You need to agree", "moved too quickly", "keepalive",
             "Perflib", "counter names", "com.sun.jna", "SystemReport.ignoreErrors")
    result.log_errors = [
        line for line in srv.errors() if not any(n.lower() in line.lower() for n in noise)
    ]
    return result


def format_result(result: VerifyResult) -> str:
    lines = []
    for c in result.checks:
        mark = "✅" if c.ok else "❌"
        lines.append(f"  {mark} {c.name}" + (f"\n       {c.detail}" if c.detail and not c.ok else ""))
    if result.log_errors:
        lines.append(f"  ❌ サーバログにエラー {len(result.log_errors)} 件")
        for line in result.log_errors[:8]:
            lines.append(f"       {line[:150]}")
    else:
        lines.append("  ✅ サーバログにエラーなし")
    lines.append("")
    lines.append("  結果: " + ("すべて通過" if result.ok else "失敗あり"))
    return "\n".join(lines)


__all__ = ["Check", "VerifyResult", "verify_layout", "format_result", "ServerError"]
