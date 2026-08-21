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


def install_datapack(world: Path, pack: Path) -> Path:
    """ワールドの ``datapacks/`` にデータパックを入れる（既存は置き換え）。"""
    dest = world / "datapacks" / pack.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(pack, dest)
    return dest
