"""Minecraft のインストール済みアセットから音符ブロック音源を取り出す。

Mojang のアセットは再配布できないので、リポジトリには同梱せず実行時に
ユーザーの ``.minecraft`` から抽出する。副次的な利点として、常に対象バージョン
そのものの音源が手に入る（26.1 で追加されたトランペット4種など）。

    python -m mcnb.mcassets --out assets/mc
    python -m mcnb.mcassets --version 26.2 --out assets/mc --no-imitate

sounds.json の解決で分かること:

* ``block.note_block.harp`` が鳴らすのは ``note/harp.ogg`` ではなく ``note/harp2.ogg``、
  ``bass`` は ``note/bassattack.ogg``。``harp.ogg`` / ``bass.ogg`` は使われていない旧ファイル。
* ``imitate.*``（Mob ヘッド）は別イベントへの参照で、サンプルが複数ある場合は
  再生のたびにランダムに選ばれる。厳密な再現には使えない。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

NOTE_BLOCK_PREFIX = "block.note_block."
IMITATE_PREFIX = "block.note_block.imitate."
MAX_EVENT_DEPTH = 8


class AssetError(RuntimeError):
    pass


@dataclass(frozen=True)
class SoundFile:
    """1サンプル。同じ楽器に複数あるならランダム選択される。"""

    sound_path: str  # note/harp2
    file: str  # 抽出先ファイル名
    sha1: str
    size: int
    volume: float = 1.0
    pitch: float = 1.0
    weight: float = 1.0


@dataclass(frozen=True)
class NoteSample:
    """音符ブロックの1音色。"""

    event: str  # block.note_block.harp
    instrument: str  # harp
    imitate: bool  # Mob ヘッド由来か
    files: list[SoundFile] = field(default_factory=list)

    @property
    def randomized(self) -> bool:
        """再生ごとにサンプルが変わるか（＝厳密な再現に使えない）。"""
        return len(self.files) > 1


@dataclass(frozen=True)
class Manifest:
    minecraft_version: str
    asset_index: str
    minecraft_dir: str
    samples: list[NoteSample]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# .minecraft の場所とバージョン
# --------------------------------------------------------------------------- #


def default_minecraft_dir() -> Path:
    """OS ごとの既定の ``.minecraft``。``MINECRAFT_DIR`` で上書きできる。"""
    env = os.environ.get("MINECRAFT_DIR")
    if env:
        return Path(env)

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if not base:
            raise AssetError("APPDATA が設定されていません。--minecraft-dir で指定してください。")
        return Path(base) / ".minecraft"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "minecraft"
    return Path.home() / ".minecraft"


def _version_meta(mc_dir: Path, version: str) -> dict | None:
    meta = mc_dir / "versions" / version / f"{version}.json"
    if not meta.is_file():
        return None
    try:
        with meta.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def list_installed_releases(mc_dir: Path) -> list[tuple[str, str]]:
    """``(version, releaseTime)`` を新しい順に。正式リリースかつ jar があるものだけ。"""
    versions_dir = mc_dir / "versions"
    if not versions_dir.is_dir():
        raise AssetError(f"versions ディレクトリが見つかりません: {versions_dir}")

    found: list[tuple[str, str]] = []
    for entry in versions_dir.iterdir():
        if not entry.is_dir():
            continue
        meta = _version_meta(mc_dir, entry.name)
        if meta is None or meta.get("type") != "release":
            continue
        # Fabric などのローダーは inheritsFrom で本体を参照するだけで、
        # jar もダウンロード情報も持たない。バニラの本体だけを拾う
        if meta.get("inheritsFrom") or "client" not in meta.get("downloads", {}):
            continue
        if not (entry / f"{entry.name}.jar").is_file():
            continue
        found.append((entry.name, meta.get("releaseTime", "")))

    found.sort(key=lambda t: t[1], reverse=True)
    return found


def resolve_version(mc_dir: Path, version: str | None = None) -> str:
    if version:
        if _version_meta(mc_dir, version) is None:
            raise AssetError(f"バージョン {version} が {mc_dir / 'versions'} にありません。")
        return version

    releases = list_installed_releases(mc_dir)
    if not releases:
        raise AssetError(
            f"{mc_dir} に正式リリース版が見つかりません。"
            "一度ランチャーで起動してから再実行するか、--version で指定してください。"
        )
    return releases[0][0]


# --------------------------------------------------------------------------- #
# アセットインデックス
# --------------------------------------------------------------------------- #


def load_asset_index(mc_dir: Path, version: str) -> tuple[str, dict[str, dict]]:
    """``(index_id, objects)``。objects は仮想パス -> {hash, size}。"""
    meta = _version_meta(mc_dir, version)
    if meta is None:
        raise AssetError(f"{version}.json が読めません。")

    index_id = meta.get("assetIndex", {}).get("id") or meta.get("assets")
    if not index_id:
        raise AssetError(f"{version} に assetIndex がありません。")

    index_path = mc_dir / "assets" / "indexes" / f"{index_id}.json"
    if not index_path.is_file():
        raise AssetError(
            f"アセットインデックス {index_path} がありません。"
            f"ランチャーで一度 {version} を起動してダウンロードさせてください。"
        )
    with index_path.open(encoding="utf-8") as f:
        return str(index_id), json.load(f)["objects"]


def read_object(mc_dir: Path, sha1: str) -> bytes:
    p = mc_dir / "assets" / "objects" / sha1[:2] / sha1
    if not p.is_file():
        raise AssetError(f"アセット実体がありません: {p}")
    return p.read_bytes()


# --------------------------------------------------------------------------- #
# sounds.json の解決
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Ref:
    """sounds.json を辿って得た、実ファイル1個への参照。"""

    sound_path: str
    volume: float
    pitch: float
    weight: float


def _resolve_event(sounds: dict, event: str, depth: int = 0) -> list[_Ref]:
    """イベント名から実ファイル参照の一覧へ。``type: event`` を再帰的に展開する。

    ``imitate.*`` は他のイベントを参照し、そのイベントが複数サンプルを持つことがある
    （skeleton は say1/say2/say3）。参照元の volume/pitch は掛け合わせて伝播させる。
    """
    if depth > MAX_EVENT_DEPTH:
        raise AssetError(f"{event}: sounds.json の参照が深すぎます（循環参照？）")

    definition = sounds.get(event)
    if definition is None:
        return []

    out: list[_Ref] = []
    for entry in definition.get("sounds") or []:
        if isinstance(entry, str):
            out.append(_Ref(entry, 1.0, 1.0, 1.0))
            continue

        name = entry.get("name")
        if not name:
            continue
        volume = float(entry.get("volume", 1.0))
        pitch = float(entry.get("pitch", 1.0))
        weight = float(entry.get("weight", 1.0))

        if entry.get("type") == "event":
            for inner in _resolve_event(sounds, name, depth + 1):
                out.append(
                    _Ref(
                        inner.sound_path,
                        inner.volume * volume,
                        inner.pitch * pitch,
                        inner.weight * weight,
                    )
                )
        else:
            out.append(_Ref(name, volume, pitch, weight))

    return out


def _sounds_json(mc_dir: Path, objects: dict[str, dict]) -> dict:
    entry = objects.get("minecraft/sounds.json")
    if entry is None:
        raise AssetError("sounds.json がアセットインデックスにありません。")
    return json.loads(read_object(mc_dir, entry["hash"]))


# --------------------------------------------------------------------------- #
# 抽出
# --------------------------------------------------------------------------- #


def extract_note_sounds(
    mc_dir: Path,
    version: str,
    out_dir: Path,
    include_imitate: bool = True,
) -> Manifest:
    """音符ブロックの音源を ``out_dir`` へ抽出し、マニフェストを返す。"""
    index_id, objects = load_asset_index(mc_dir, version)
    sounds = _sounds_json(mc_dir, objects)

    out_dir.mkdir(parents=True, exist_ok=True)
    samples: list[NoteSample] = []

    for event in sorted(sounds):
        if not event.startswith(NOTE_BLOCK_PREFIX):
            continue
        imitate = event.startswith(IMITATE_PREFIX)
        if imitate and not include_imitate:
            continue

        instrument = event[len(NOTE_BLOCK_PREFIX) :]
        refs = _resolve_event(sounds, event)
        if not refs:
            print(f"  ! {event}: サンプルを特定できずスキップ", file=sys.stderr)
            continue

        files: list[SoundFile] = []
        for i, ref in enumerate(refs):
            virtual = f"minecraft/sounds/{ref.sound_path}.ogg"
            entry = objects.get(virtual)
            if entry is None:
                print(f"  ! {event}: {virtual} がインデックスにありません", file=sys.stderr)
                continue

            name = f"{instrument}.ogg" if len(refs) == 1 else f"{instrument}__{i}.ogg"
            (out_dir / name).write_bytes(read_object(mc_dir, entry["hash"]))
            files.append(
                SoundFile(
                    sound_path=ref.sound_path,
                    file=name,
                    sha1=entry["hash"],
                    size=entry["size"],
                    volume=ref.volume,
                    pitch=ref.pitch,
                    weight=ref.weight,
                )
            )

        if files:
            samples.append(NoteSample(event=event, instrument=instrument, imitate=imitate, files=files))

    if not samples:
        raise AssetError("音符ブロックの音源が1つも取り出せませんでした。")

    manifest = Manifest(
        minecraft_version=version,
        asset_index=index_id,
        minecraft_dir=str(mc_dir),
        samples=samples,
    )
    (out_dir / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    return manifest


def load_manifest(out_dir: Path) -> Manifest:
    """抽出済みディレクトリから Manifest を読み戻す。"""
    path = out_dir / "manifest.json"
    if not path.is_file():
        raise AssetError(f"{path} がありません。先に python -m mcnb.mcassets を実行してください。")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Manifest(
        minecraft_version=raw["minecraft_version"],
        asset_index=raw["asset_index"],
        minecraft_dir=raw["minecraft_dir"],
        samples=[
            NoteSample(
                event=s["event"],
                instrument=s["instrument"],
                imitate=s["imitate"],
                files=[SoundFile(**f) for f in s["files"]],
            )
            for s in raw["samples"]
        ],
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _describe(sample: NoteSample) -> str:
    head = sample.files[0]
    bits = []
    if head.sound_path != f"note/{sample.instrument}":
        bits.append(f"← {head.sound_path}")
    if sample.randomized:
        bits.append(f"ランダム{len(sample.files)}種")
    if head.pitch != 1.0:
        bits.append(f"pitch×{head.pitch:g}")
    if head.volume != 1.0:
        bits.append(f"vol×{head.volume:g}")
    return ("  " + " / ".join(bits)) if bits else ""


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Minecraft から音符ブロック音源を抽出する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--minecraft-dir", type=Path, default=None, help="既定: OS ごとの .minecraft")
    ap.add_argument("--version", default=None, help="既定: インストール済みの最新正式リリース")
    ap.add_argument("--out", type=Path, default=Path("assets/mc"), help="抽出先 (既定: assets/mc)")
    ap.add_argument("--no-imitate", action="store_true", help="Mob ヘッド音を除外する")
    ap.add_argument("--list-versions", action="store_true", help="インストール済みリリースを一覧表示")
    ap.add_argument("--force", action="store_true", help="抽出先が空でなくても上書きする")
    args = ap.parse_args(argv)

    try:
        mc_dir = args.minecraft_dir or default_minecraft_dir()
        if not mc_dir.is_dir():
            raise AssetError(f"Minecraft が見つかりません: {mc_dir}")

        if args.list_versions:
            for name, released in list_installed_releases(mc_dir):
                print(f"{name:12s} {released}")
            return 0

        version = resolve_version(mc_dir, args.version)
        out: Path = args.out

        if out.exists() and any(out.iterdir()) and not args.force:
            existing = out / "manifest.json"
            if existing.is_file():
                prev = json.loads(existing.read_text(encoding="utf-8"))
                if prev.get("minecraft_version") == version:
                    print(f"{out} に {version} の音源が既にあります (--force で再抽出)")
                    return 0
            raise AssetError(f"{out} が空ではありません。--force を付けるか別の --out を指定してください。")

        if args.force and out.exists():
            shutil.rmtree(out)

        print(f"Minecraft : {mc_dir}")
        print(f"バージョン: {version}")
        manifest = extract_note_sounds(mc_dir, version, out, include_imitate=not args.no_imitate)

        playable = [s for s in manifest.samples if not s.imitate]
        imitate = [s for s in manifest.samples if s.imitate]
        print(f"アセットindex: {manifest.asset_index}")
        print(f"抽出先: {out}")
        print(f"\n音符ブロック楽器 {len(playable)} 種:")
        for s in playable:
            print(f"  {s.instrument:20s} {s.files[0].size:>6d} B{_describe(s)}")
        if imitate:
            print(f"\nMob ヘッド音 {len(imitate)} 種:")
            for s in imitate:
                print(f"  {s.instrument:20s} {s.files[0].size:>6d} B{_describe(s)}")

    except AssetError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
