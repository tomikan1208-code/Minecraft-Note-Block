"""このプロジェクト専用の Minecraft 環境（Fabric + 軽量化 Mod）を用意する。

    uv run python -m mcnb.mcmods --setup

やること:

1. Fabric Loader のバージョン JSON をランチャーの ``versions/`` に置く
   （Fabric の公式 meta API が完成済みの JSON を返すので、**インストーラ jar を
   ダウンロードして実行する必要はない**。ライブラリの取得はランチャーがやる）
2. ランチャーのプロファイルを、このプロジェクトの ``.minecraft`` を
   ゲームディレクトリとして Fabric 版で起動するように書き換える
3. Mod を Modrinth から ``<project>/.minecraft/mods/`` へ落とす（SHA1 検証つき）

ゲームディレクトリとランチャーディレクトリは別物であることに注意:

* ランチャー側 (``%APPDATA%/.minecraft``) — ``versions/`` ``libraries/`` ``assets/``
* ゲーム側 (このプロジェクトの ``.minecraft``) — ``mods/`` ``saves/`` ``options.txt``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .mcassets import AssetError, default_minecraft_dir

FABRIC_META = "https://meta.fabricmc.net/v2"
MODRINTH_API = "https://api.modrinth.com/v2"
USER_AGENT = "mcnb/0.1 (github.com/tomikan1208-code/Minecraft-Note-Block)"

#: 既定で入れる Mod。理由をコメントに書いておく（後で外す判断ができるように）
DEFAULT_MODS = [
    "fabric-api",       # ほぼ全ての Mod の前提
    "lithium",          # サーバ側 tick の最適化。datapack で大量の setblock を打つので効く
    "sodium",           # 描画の軽量化
    "ferrite-core",     # メモリ使用量の削減
    "immediatelyfast",  # 描画まわりの軽量化
    "rsls",             # Raise Sound Limit Simplified — 同時発音を 4095 まで上げる（必須）
]


class ModError(RuntimeError):
    pass


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _get_json(url: str):
    return json.loads(_get(url))


# --------------------------------------------------------------------------- #
# Fabric Loader
# --------------------------------------------------------------------------- #


def latest_fabric_loader(mc_version: str) -> str:
    entries = _get_json(f"{FABRIC_META}/versions/loader/{urllib.parse.quote(mc_version)}")
    if not entries:
        raise ModError(f"Fabric が Minecraft {mc_version} に対応していません。")
    for e in entries:
        if e["loader"].get("stable"):
            return e["loader"]["version"]
    return entries[0]["loader"]["version"]


def install_fabric(launcher_dir: Path, mc_version: str, loader: str | None = None) -> str:
    """Fabric のバージョン JSON を ``versions/`` に配置し、その id を返す。"""
    loader = loader or latest_fabric_loader(mc_version)
    profile = _get_json(
        f"{FABRIC_META}/versions/loader/"
        f"{urllib.parse.quote(mc_version)}/{urllib.parse.quote(loader)}/profile/json"
    )
    version_id = profile["id"]
    dest_dir = launcher_dir / "versions" / version_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{version_id}.json").write_text(
        json.dumps(profile, indent=2), encoding="utf-8"
    )
    return version_id


def configure_launcher_profile(
    launcher_dir: Path,
    game_dir: Path,
    version_id: str,
    name: str = "mcnb (音ブロック)",
) -> str:
    """ゲームディレクトリが ``game_dir`` のプロファイルを Fabric 版に向ける。

    既存のプロファイルがあれば書き換え、無ければ作る。書き換える前にバックアップを取る。
    """
    path = launcher_dir / "launcher_profiles.json"
    if not path.is_file():
        raise ModError(f"{path} がありません。ランチャーを一度起動してください。")

    backup = path.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)

    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = data.setdefault("profiles", {})
    target = str(game_dir)

    key = None
    for k, v in profiles.items():
        if v.get("gameDir") and Path(v["gameDir"]) == game_dir:
            key = k
            break

    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    if key is None:
        key = hashlib.sha1(target.encode("utf-8")).hexdigest()[:32]
        profiles[key] = {"created": now, "type": "custom"}

    profiles[key].update(
        {
            "name": name,
            "gameDir": target,
            "lastVersionId": version_id,
            "type": "custom",
            "lastUsed": now,
        }
    )
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(backup)


# --------------------------------------------------------------------------- #
# Modrinth
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModFile:
    slug: str
    project_name: str
    version_number: str
    filename: str
    url: str
    sha1: str
    size: int
    version_type: str  # release / beta / alpha


def find_mod(slug: str, mc_version: str, loader: str = "fabric") -> ModFile | None:
    """指定バージョン向けの最新ファイルを1つ返す。無ければ None。"""
    query = urllib.parse.urlencode(
        {
            "game_versions": json.dumps([mc_version]),
            "loaders": json.dumps([loader]),
        }
    )
    versions = _get_json(f"{MODRINTH_API}/project/{urllib.parse.quote(slug)}/version?{query}")
    if not versions:
        return None

    # release > beta > alpha を優先し、同順位なら API が返した順（新しい順）
    rank = {"release": 0, "beta": 1, "alpha": 2}
    versions.sort(key=lambda v: rank.get(v.get("version_type", "release"), 3))
    v = versions[0]

    primary = next((f for f in v["files"] if f.get("primary")), v["files"][0])
    return ModFile(
        slug=slug,
        project_name=v.get("name", slug),
        version_number=v["version_number"],
        filename=primary["filename"],
        url=primary["url"],
        sha1=primary["hashes"]["sha1"],
        size=primary["size"],
        version_type=v.get("version_type", "release"),
    )


def download_mod(mod: ModFile, mods_dir: Path) -> tuple[Path, bool]:
    """``(保存先, ダウンロードしたか)``。既にあって SHA1 が一致すれば何もしない。"""
    mods_dir.mkdir(parents=True, exist_ok=True)
    dest = mods_dir / mod.filename

    if dest.is_file() and hashlib.sha1(dest.read_bytes()).hexdigest() == mod.sha1:
        return dest, False

    blob = _get(mod.url)
    got = hashlib.sha1(blob).hexdigest()
    if got != mod.sha1:
        raise ModError(f"{mod.filename}: SHA1 不一致（期待 {mod.sha1} / 実際 {got}）。破棄しました。")

    # 同じ Mod の古いファイルを片付ける
    for old in mods_dir.glob("*.jar"):
        if old.name != mod.filename and old.name.lower().startswith(mod.slug.split("-")[0].lower()):
            old.unlink()

    dest.write_bytes(blob)
    return dest, True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _project_game_dir() -> Path:
    """このリポジトリ直下の .minecraft（プロジェクト専用のゲームディレクトリ）。"""
    return Path(__file__).resolve().parents[2] / ".minecraft"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Fabric と軽量化 Mod をこのプロジェクト用に導入する")
    ap.add_argument("--game-dir", type=Path, default=None, help="既定: <repo>/.minecraft")
    ap.add_argument("--launcher-dir", type=Path, default=None, help="既定: OS ごとの .minecraft")
    ap.add_argument("--mc", default="26.2", help="対象の Minecraft バージョン")
    ap.add_argument("--mods", nargs="*", default=None, help="Modrinth の slug（既定は軽量化一式）")
    ap.add_argument("--no-fabric", action="store_true", help="Fabric の導入をスキップし Mod だけ入れる")
    ap.add_argument("--no-launcher", action="store_true", help="launcher_profiles.json を触らない")
    ap.add_argument("--setup", action="store_true", help="全部やる（既定）")
    args = ap.parse_args(argv)

    try:
        game_dir = args.game_dir or _project_game_dir()
        launcher_dir = args.launcher_dir or default_minecraft_dir()
        game_dir.mkdir(parents=True, exist_ok=True)

        print(f"Minecraft   : {args.mc}")
        print(f"ゲーム側    : {game_dir}")
        print(f"ランチャー側: {launcher_dir}")

        version_id = None
        if not args.no_fabric:
            loader = latest_fabric_loader(args.mc)
            version_id = install_fabric(launcher_dir, args.mc, loader)
            print(f"\nFabric Loader {loader} を導入: {version_id}")
            print(f"  → {launcher_dir / 'versions' / version_id}")

            if not args.no_launcher:
                backup = configure_launcher_profile(launcher_dir, game_dir, version_id)
                print("  ランチャープロファイルを更新（起動時に選べます）")
                print(f"  バックアップ: {backup}")

        slugs = args.mods if args.mods is not None else DEFAULT_MODS
        if slugs:
            print(f"\nMod を {game_dir / 'mods'} へ:")
            missing: list[str] = []
            for slug in slugs:
                mod = find_mod(slug, args.mc)
                if mod is None:
                    missing.append(slug)
                    print(f"  ✗ {slug:18s} {args.mc} 対応版なし")
                    continue
                _, fetched = download_mod(mod, game_dir / "mods")
                mark = "↓" if fetched else "="
                warn = f"  ⚠{mod.version_type}" if mod.version_type != "release" else ""
                print(f"  {mark} {slug:18s} {mod.version_number:28s} {mod.size / 1024:7.0f} KB{warn}")
            if missing:
                print(f"\n  未対応: {', '.join(missing)}")

        print("\n完了。ランチャーで「mcnb (音ブロック)」プロファイルを選んで起動してください。")
        if version_id:
            print(f"（ランチャーが開いていた場合は再起動が必要です。バージョン: {version_id}）")

    except (ModError, AssetError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"ネットワークエラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
