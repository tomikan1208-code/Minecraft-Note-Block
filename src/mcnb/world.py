"""演奏専用のワールドを新規生成する。

既存のワールドを書き換えない。チートON・クリエイティブ・デフォルトのフラット地形の
まっさらなワールドを毎回作って、そこにデータパックを入れる。

level.dat を手で書き起こすとバージョンごとの差分で壊れやすいので、
**headless サーバに生成させてから必要な項目だけ書き換える**。
サーバが作るワールドは単一プレイヤーの保存形式と同じ構成
（``level.dat`` / ``region`` / ``DIM-1`` / ``DIM1``）なので、そのまま saves に置ける。

    uv run mcnb world --name mcnb_test
    uv run mcnb build song.mid --world mcnb_song
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .mcassets import default_minecraft_dir, resolve_version
from .server import Server, ServerError, ensure_server_jar, find_java

#: バニラの「クラシックフラット」と同じ層。
#: generator-settings を省くとサーバが {} を書き、それは **層なし＝ボイド** になる。
#: 「空中は嫌」なので明示的に地面を作る。
CLASSIC_FLAT = {
    "layers": [
        {"block": "minecraft:bedrock", "height": 1},
        {"block": "minecraft:dirt", "height": 2},
        {"block": "minecraft:grass_block", "height": 1},
    ],
    "biome": "minecraft:plains",
    # 演奏の邪魔になるものを全部切る。
    # 村や要塞が構造の中に生えてくると音符ブロックを壊すし、視界も邪魔になる
    "structure_overrides": [],
    "lakes": False,
    "features": False,
}

#: ワールドに焼き込むゲームルール。26.x の名前（camelCase から変わっている）
WORLD_GAMERULES = {
    "spawn_mobs": "false",          # 旧 doMobSpawning
    "spawn_monsters": "false",
    "spawn_patrols": "false",
    "spawn_phantoms": "false",
    "spawn_wandering_traders": "false",
    "spawn_wardens": "false",
    "spawner_blocks_work": "false",
    "mob_griefing": "false",
    "advance_time": "false",        # 旧 doDaylightCycle
    "advance_weather": "false",     # 旧 doWeatherCycle
    "random_tick_speed": "0",
    "fire_damage": "false",
    "fall_damage": "false",
    "drowning_damage": "false",
    "freeze_damage": "false",
    "immediate_respawn": "true",
    "show_death_messages": "false",
    "max_command_sequence_length": "2147483647",
    "max_block_modifications": "2147483647",
    "send_command_feedback": "false",
    "command_block_output": "false",
}
#: 草ブロックの上、プレイヤーが立つ高さ。岩盤(-64)+土(-63,-62)+草(-61) なので -60
FLAT_SURFACE_Y = -60


@dataclass(frozen=True)
class WorldResult:
    path: Path
    name: str
    created: bool


def _project_game_dir() -> Path:
    return Path(__file__).resolve().parents[2] / ".minecraft"


def _patch_level_dat(path: Path, name: str, spawn: tuple[int, int, int]) -> None:
    """単一プレイヤーで開けるように level.dat を書き換える。

    サーバが作った level.dat はチートOFF・サバイバル相当なので、
    ``allowCommands`` と ``GameType`` を立てないと ``/function`` が使えない。
    """
    import nbtlib

    level = nbtlib.load(str(path))
    data = level["Data"] if "Data" in level else level

    data["allowCommands"] = nbtlib.Byte(1)   # チートON
    data["GameType"] = nbtlib.Int(1)         # クリエイティブ
    data["Difficulty"] = nbtlib.Byte(0)      # ピースフル
    data["LevelName"] = nbtlib.String(name)
    x, y, z = spawn
    data["SpawnX"] = nbtlib.Int(x)
    data["SpawnY"] = nbtlib.Int(y)
    data["SpawnZ"] = nbtlib.Int(z)
    level.save()


def create_world(
    name: str,
    game_dir: Path | None = None,
    datapack: Path | None = None,
    spawn: tuple[int, int, int] = (-10, FLAT_SURFACE_Y, 0),
    mc: str | None = None,
    overwrite: bool = False,
    memory: str = "2G",
) -> WorldResult:
    """``<game_dir>/saves/<name>`` に演奏用ワールドを作る。"""
    game_dir = game_dir or _project_game_dir()
    launcher = default_minecraft_dir()
    mc = mc or resolve_version(launcher)

    dest = game_dir / "saves" / name
    if dest.exists():
        if not overwrite:
            return WorldResult(path=dest, name=name, created=False)
        shutil.rmtree(dest)

    # 生成専用の作業ディレクトリ。検証用サーバとは分けておく
    root = game_dir / "worldgen"
    staging = root / name
    if staging.exists():
        shutil.rmtree(staging)

    java = find_java()
    # jar は検証用サーバと共用する（58MB を二重に持たない）
    jar = ensure_server_jar(game_dir / "server", mc, launcher)

    srv = Server(root, jar, java, memory=memory)
    srv.configure(
        accept_eula=True,
        level_name=name,
        extra={
            "level-type": "minecraft:flat",
            "generator-settings": json.dumps(CLASSIC_FLAT),
            "spawn-protection": "0",
        },
    )
    srv.start()
    # ゲームルールはサーバ上で設定してから止めると level.dat に焼き込まれる。
    # あとから手で設定しなくても、開いた時点で静かなワールドになる
    _apply_gamerules()
    # 起動直後だと spawn 周辺のチャンク書き出しが終わっていないことがある
    time.sleep(2.0)
    srv.stop()

    level_dat = staging / "level.dat"
    if not level_dat.is_file():
        raise ServerError(f"ワールドが生成されませんでした: {staging}")

    _patch_level_dat(level_dat, name, spawn)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(staging, dest)
    shutil.rmtree(staging)

    if datapack is not None:
        install_datapack(dest, datapack)

    return WorldResult(path=dest, name=name, created=True)


def _apply_gamerules() -> None:
    """RCON でゲームルールを設定する。止めるときに level.dat へ保存される。"""
    from .server import Rcon

    with Rcon() as rcon:
        for rule, value in WORLD_GAMERULES.items():
            response = rcon.command(f"gamerule {rule} {value}")
            if "Incorrect argument" in response or "Unknown" in response:
                print(f"  ! ゲームルール {rule} は 26.2 に無いようです")
        # 既にいる分も消しておく
        rcon.command("kill @e[type=!player]")
        rcon.command("time set noon")
        rcon.command("weather clear 1000000")


def install_datapack(world: Path, pack: Path) -> Path:
    """ワールドの ``datapacks/`` にデータパックを入れる（既存は置き換え）。"""
    dest = world / "datapacks" / pack.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(pack, dest)
    return dest
