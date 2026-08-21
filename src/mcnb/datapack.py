"""配置結果を Minecraft のデータパックとして書き出す。

Mod を必要とせず、バニラのコマンドだけで動く。トロッコもレールも使わない。

演奏の仕組み:

* ``minecraft:tick`` タグの関数が毎 game tick カウンタを進め、
  マクロで ``mcnb:t/<tick>`` を呼ぶ
* ``mcnb:t/<tick>`` は (1) プレイヤーをその tick の位置へ ``/tp`` し、
  (2) 前 tick の発火用レッドストーンを消し、(3) この tick のぶんを置く
* 音符ブロックは事前に ``mcnb:build`` で全部設置しておく

構造が数千ブロックに及ぶので、設置も再生も ``/forceload`` の窓を動かしながら行う。
未読み込みチャンクへの ``/setblock`` は黙って失敗するため、これは必須。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .instruments import INSTRUMENTS, block_state
from .layout import Layout, Placement

#: Minecraft 26.2 の data pack format。26.x は major.minor 制で、
#: 82 以降を宣言するパックは min_format / max_format が必須（pack_format だけだと弾かれる）
PACK_FORMAT_MAJOR = 107
PACK_FORMAT_MINOR = 1

#: 26.x でゲームルールの名前が全面的に変わった（camelCase → snake_case、一部は改名）。
#: 実機のコマンドツリーから確認した対応:
#:   maxCommandChainLength -> max_command_sequence_length
#:   doDaylightCycle       -> advance_time
#:   doWeatherCycle        -> advance_weather
#:   doMobSpawning         -> spawn_mobs
#:   commandModificationBlockLimit -> max_block_modifications
SETUP_COMMANDS = [
    "# 大量の setblock / fill を通すための設定",
    "gamerule max_command_sequence_length 2147483647",
    "gamerule max_block_modifications 2147483647",
    "gamerule command_block_output false",
    "gamerule send_command_feedback false",
    "gamerule advance_time false",
    "gamerule advance_weather false",
    "gamerule spawn_mobs false",
    "gamerule random_tick_speed 0",
    "time set noon",
    "weather clear",
]

#: +X（東）を向く yaw
YAW_EAST = -90.0

#: 1回の build 関数に詰めるコマンド数の上限
BUILD_BATCH_COMMANDS = 6000
#: 1回の build 関数がカバーする X 幅（ブロック）。forceload のチャンク数を抑えるため
BUILD_BATCH_SPAN = 256
#: forceload してから setblock までに待つ tick 数。チャンク読み込みは非同期
CHUNK_LOAD_DELAY = 10
#: 再生中に forceload する窓の幅（ブロック）と、張り替える間隔（tick）
PLAY_WINDOW = 192
PLAY_WINDOW_TICKS = 48

NS = "mcnb"


@dataclass(frozen=True)
class DatapackResult:
    path: Path
    functions: int
    commands: int
    build_parts: int


def _forceload_span(x1: int, z1: int, x2: int, z2: int) -> str:
    """``/forceload add <from_x> <from_z> <to_x> <to_z>``。"""
    return f"forceload add {x1} {z1} {x2} {z2}"


class _Writer:
    def __init__(self, root: Path):
        self.root = root
        self.count = 0
        self.commands = 0

    def write(self, rel: str, lines: list[str]) -> None:
        path = self.root / "data" / NS / "function" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.count += 1
        self.commands += sum(1 for line in lines if line and not line.startswith("#"))


# --------------------------------------------------------------------------- #
# 構造の設置
# --------------------------------------------------------------------------- #


def _build_commands(placements: list[Placement]) -> list[str]:
    """音符ブロックと楽器ブロックを置くコマンド。真上の空気も確保する。"""
    out: list[str] = []
    for p in placements:
        block = INSTRUMENTS[p.instrument].block
        ix, iy, iz = p.instrument_block
        out.append(f"setblock {ix} {iy} {iz} minecraft:{block} replace")
        out.append(f"setblock {p.x} {p.y} {p.z} {block_state(p.instrument, p.key)} replace")
        out.append(f"setblock {p.x} {p.y + 1} {p.z} minecraft:air replace")
    return out


def _split_builds(layout: Layout) -> list[list[Placement]]:
    """X 幅とコマンド数の両方で build を分割する。"""
    by_tick = layout.placements_by_tick()
    batches: list[list[Placement]] = []
    current: list[Placement] = []
    current_x0: int | None = None

    for tick in sorted(by_tick):
        group = by_tick[tick]
        x = group[0].x
        if current_x0 is None:
            current_x0 = x
        too_wide = x - current_x0 >= BUILD_BATCH_SPAN
        too_many = len(current) * 3 + len(group) * 3 > BUILD_BATCH_COMMANDS
        if current and (too_wide or too_many):
            batches.append(current)
            current = []
            current_x0 = x
        current.extend(group)

    if current:
        batches.append(current)
    return batches


# --------------------------------------------------------------------------- #
# 操作盤
# --------------------------------------------------------------------------- #

#: (ラベル色, 呼ぶ関数)。色ブロックがそのままボタンの意味になる
PANEL_BUTTONS = [
    ("lime_concrete", "build", "設置"),
    ("light_blue_concrete", "play", "演奏"),
    ("red_concrete", "stop", "停止"),
    ("yellow_concrete", "goto_start", "開始位置へ"),
]

#: 操作盤を置く位置（演奏開始位置からの X オフセット）
PANEL_OFFSET_X = -10


def _panel_commands(x0: int, y0: int, z0: int) -> list[str]:
    """立って押せるコマンドブロックの操作盤を組む。

    上から見ると Z 方向に 4 個並ぶ。足元の色つきコンクリートがボタンのラベル::

        黄 = 開始位置へ / 赤 = 停止 / 水色 = 演奏 / 黄緑 = 設置
    """
    px = x0 + PANEL_OFFSET_X
    lines = [
        "# 操作盤",
        f"forceload add {px - 4} {z0 - 8} {x0 + 4} {z0 + 8}",
        # 立つ床
        f"fill {px - 2} {y0 - 1} {z0 - 4} {px + 2} {y0 - 1} {z0 + 4} minecraft:smooth_stone",
        f"fill {px - 2} {y0} {z0 - 4} {px + 2} {y0 + 2} {z0 + 4} minecraft:air",
    ]

    for i, (colour, fn, _label) in enumerate(PANEL_BUTTONS):
        z = z0 - 3 + i * 2
        lines.append(f"setblock {px} {y0 - 1} {z} minecraft:{colour} replace")
        # コマンドブロックから実行すると @s はプレイヤーにならないので、
        # 必ず execute as @p で包んでから関数を呼ぶ
        command = f"execute as @p at @s run function {NS}:{fn}"
        lines.append(
            f"setblock {px + 1} {y0 - 1} {z} "
            f'minecraft:command_block[facing=up]{{Command:"{command}",auto:0b,TrackOutput:0b}} replace'
        )
        lines.append(
            f"setblock {px + 1} {y0} {z} minecraft:stone_button[face=floor,facing=east] replace"
        )

    lines.append(f"tp @p {px} {y0} {z0} 90 0")
    lines.append('tellraw @a {"text":"[mcnb] 操作盤: 黄緑=設置 / 水色=演奏 / 赤=停止 / 黄=開始位置へ"}')
    return lines


# --------------------------------------------------------------------------- #
# 本体
# --------------------------------------------------------------------------- #


def emit(layout: Layout, out_dir: Path, name: str | None = None, overwrite: bool = True) -> DatapackResult:
    """データパックを ``out_dir`` に書き出す。"""
    name = name or layout.song.name
    root = Path(out_dir)
    if root.exists() and overwrite:
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    (root / "pack.mcmeta").write_text(
        json.dumps(
            {
                "pack": {
                    "description": f"mcnb — {name}（音符ブロック自動編曲）",
                    "min_format": [PACK_FORMAT_MAJOR, PACK_FORMAT_MINOR],
                    "max_format": PACK_FORMAT_MAJOR,
                }
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    for tag, fn in (("load", f"{NS}:load"), ("tick", f"{NS}:tick_root")):
        p = root / "data" / "minecraft" / "tags" / "function" / f"{tag}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"values": [fn]}, indent=2), encoding="utf-8")

    w = _Writer(root)
    x0, y0, z0 = layout.origin
    total_ticks = layout.song.length_ticks
    (bx1, by1, bz1), (bx2, by2, bz2) = layout.bounds()

    # --- load / setup ------------------------------------------------------ #
    w.write(
        "load.mcfunction",
        [
            f"# {name} — mcnb",
            f"scoreboard objectives add {NS} dummy",
            f"scoreboard players set #playing {NS} 0",
            f"scoreboard players set #t {NS} 0",
            f"scoreboard players set #built {NS} 0",
            f"scoreboard players set #length {NS} {total_ticks + 1}",
            f'tellraw @a {{"text":"[mcnb] {name} 読み込み完了。/function {NS}:panel で操作盤を出す"}}',
        ],
    )

    w.write("setup.mcfunction", SETUP_COMMANDS)

    # --- 操作盤（コマンドブロック + ボタン）--------------------------------- #
    w.write("panel.mcfunction", _panel_commands(x0, y0, z0))

    # --- build ------------------------------------------------------------- #
    #
    # /forceload add は**同じ tick ではチャンクを読み込まない**。読み込みは非同期で、
    # 直後に setblock を打っても "That position is not loaded" で黙って失敗する。
    # そのため 1区画ごとに「forceload する関数」と「設置する関数」を分け、
    # schedule で数 tick 待ってから設置する。
    #
    batches = _split_builds(layout)
    for i, batch in enumerate(batches):
        xs = [p.x for p in batch]
        w.write(
            f"build/{i}_load.mcfunction",
            [
                f"# 区画 {i + 1}/{len(batches)} のチャンクを読み込む",
                "forceload remove all",
                _forceload_span(min(xs) - 2, bz1 - 1, max(xs) + 1, bz2 + 1),
                f"schedule function {NS}:build/{i} {CHUNK_LOAD_DELAY}t replace",
            ],
        )

        lines = [f"# 区画 {i + 1}/{len(batches)} を設置"]
        lines.extend(_build_commands(batch))
        if i + 1 < len(batches):
            lines.append(f"schedule function {NS}:build/{i + 1}_load 1t replace")
        else:
            lines.append("forceload remove all")
            lines.append(f"scoreboard players set #built {NS} 1")
            lines.append('tellraw @a {"text":"[mcnb] 設置完了。水色のボタンで演奏"}')
        w.write(f"build/{i}.mcfunction", lines)

    w.write(
        "build.mcfunction",
        [
            f"function {NS}:setup",
            f"scoreboard players set #built {NS} 0",
            f'tellraw @a {{"text":"[mcnb] 設置開始… {len(batches)} 区画 / {layout.block_count} ブロック"}}',
            f"function {NS}:build/0_load",
        ],
    )

    # --- 演奏 -------------------------------------------------------------- #
    w.write(
        "play.mcfunction",
        [
            "tag @s add mcnb_listener",
            f"scoreboard players set #t {NS} 0",
            f"scoreboard players set #playing {NS} 1",
            "forceload remove all",
            _forceload_span(x0 - 2, bz1 - 1, x0 + PLAY_WINDOW, bz2 + 1),
            f"tp @s {x0} {y0} {z0} {YAW_EAST:g} 0",
            'gamemode spectator @s',
            'tellraw @a {"text":"[mcnb] 演奏開始"}',
        ],
    )

    w.write(
        "stop.mcfunction",
        [
            f"scoreboard players set #playing {NS} 0",
            "forceload remove all",
            "tag @a remove mcnb_listener",
            'tellraw @a {"text":"[mcnb] 停止"}',
        ],
    )

    w.write(
        "goto_start.mcfunction",
        [
            f"tp @s {x0} {y0} {z0} {YAW_EAST:g} 0",
            'gamemode spectator @s',
        ],
    )

    # tick ルート: マクロで t/<n> を呼ぶ
    w.write(
        "tick_root.mcfunction",
        [
            f"execute unless score #playing {NS} matches 1 run return 0",
            f"execute store result storage {NS}:state t int 1 run scoreboard players get #t {NS}",
            f"function {NS}:dispatch with storage {NS}:state",
            f"scoreboard players add #t {NS} 1",
            f"execute if score #t {NS} >= #length {NS} run function {NS}:stop",
        ],
    )
    w.write("dispatch.mcfunction", [f"$function {NS}:t/$(t)"])

    # --- 各 tick ----------------------------------------------------------- #
    # 最後に 1 tick 余分に回して、最終 tick の発火用ブロックを片付ける
    by_tick = layout.placements_by_tick()
    for tick in range(total_ticks + 1):
        px, py, pz = layout.player_pos(tick)
        lines = [f"tp @a[tag=mcnb_listener] {px} {py} {pz} {YAW_EAST:g} 0"]

        # 前 tick の発火用レッドストーンを片付ける。実際に置いた範囲だけ fill する
        prev = by_tick.get(tick - 1) if tick > 0 else None
        if prev:
            prev_x = layout.player_pos(tick - 1)[0] - 1
            ys = [p.y for p in prev]
            zs = [p.z for p in prev]
            lines.append(
                f"fill {prev_x} {min(ys)} {min(zs)} {prev_x} {max(ys)} {max(zs)} "
                "minecraft:air replace minecraft:redstone_block"
            )

        for p in by_tick.get(tick, []):
            tx, ty, tz = p.trigger_pos
            lines.append(f"setblock {tx} {ty} {tz} minecraft:redstone_block replace")

        # 先読みで forceload の窓を張り替える
        if tick % PLAY_WINDOW_TICKS == 0:
            lines.append("forceload remove all")
            lines.append(_forceload_span(px - 4, bz1 - 1, px + PLAY_WINDOW, bz2 + 1))

        w.write(f"t/{tick}.mcfunction", lines)

    return DatapackResult(path=root, functions=w.count, commands=w.commands, build_parts=len(batches))


def summarize(result: DatapackResult) -> str:
    return (
        f"データパック: {result.path}\n"
        f"  関数        : {result.functions}\n"
        f"  コマンド    : {result.commands}\n"
        f"  設置区画    : {result.build_parts}"
    )
