"""mcnb の CLI。

    uv run mcnb setup                      # Fabric + 軽量化 Mod + 音源抽出
    uv run mcnb build song.mp3             # 音源 → データパック
    uv run mcnb build song.mid --name test
    uv run mcnb info out/song/song.nbs     # NBS の中身を見る
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import datapack, layout, mcassets, mcmods, samples, song

DEFAULT_OUT = Path("out")
#: hyperchoron が直接読める入力
HYPERCHORON_INPUTS = {".mid", ".midi", ".csv", ".org", ".hpc", ".xm", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac"}


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def run_hyperchoron(src: Path, dest: Path, extra: list[str] | None = None, quiet: bool = False) -> None:
    """hyperchoron で入力を .nbs へ変換する。

    音楽的な変換（分離・採譜・楽器割り当て・strum・音量）は全部あちらに任せ、
    Minecraft 内の配置とタイミングだけをこちらでやる。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "hyperchoron",
        "-i", str(src),
        "-o", str(dest),
        "-r", "20",             # Minecraft の game tick は 20Hz。既定の 40Hz だと倍速になる
        "--strict-tempo",       # tick 境界にきっちり合わせる
        "--no-microtones",      # バニラで鳴る範囲に留める
        *(extra or []),
    ]
    if not quiet:
        print("  $ " + " ".join(cmd[2:]))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not dest.is_file():
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        raise RuntimeError(f"hyperchoron が失敗しました (exit {proc.returncode})")


@dataclass
class BuildResult:
    name: str
    song: song.Song
    layout: layout.Layout
    pack: datapack.DatapackResult
    nbs: Path


def build_one(
    src: Path,
    out_root: Path,
    name: str | None = None,
    origin: tuple[int, int, int] = (0, 100, 0),
    spacing: int = layout.SPACING,
    max_polyphony: int = 200,
    verbose: bool = True,
) -> BuildResult:
    """1つの入力を最後まで通す。CLI からもテストランナーからも使う。"""
    name = name or src.stem
    out = out_root / name
    out.mkdir(parents=True, exist_ok=True)
    nbs_path = out / f"{name}.nbs"

    if src.suffix.lower() == ".nbs":
        if nbs_path.resolve() != src.resolve():
            shutil.copy2(src, nbs_path)
    elif src.suffix.lower() in HYPERCHORON_INPUTS:
        if verbose:
            print("\n■ hyperchoron で音楽的変換 → .nbs")
        run_hyperchoron(src, nbs_path, quiet=not verbose)
    else:
        raise ValueError(f"未対応の拡張子: {src.suffix}")

    tune = song.load_nbs(nbs_path, name=name)
    if verbose:
        print("\n■ 演奏データ")
        print(song.summarize(tune))

    lay = layout.build_layout(tune, origin=origin, spacing=spacing, max_polyphony=max_polyphony)
    if verbose:
        print("\n■ Minecraft 空間に配置（直線コリドー）")
        print(layout.summarize(lay))

    pack = datapack.emit(lay, out / f"{name}_datapack", name=name)
    if verbose:
        print("\n■ データパック")
        print(datapack.summarize(pack))

    return BuildResult(name=name, song=tune, layout=lay, pack=pack, nbs=nbs_path)


def cmd_build(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.is_file():
        print(f"エラー: {src} がありません", file=sys.stderr)
        return 1

    print(f"■ 入力: {src}")
    try:
        result = build_one(
            src,
            Path(args.out),
            name=args.name,
            origin=tuple(args.origin),
            spacing=args.spacing,
            max_polyphony=args.max_polyphony,
        )
    except (ValueError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    pack_dir = result.pack.path
    if args.install:
        target = Path(args.install)
        dest = target / pack_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(pack_dir, dest)
        print(f"\n■ 導入: {dest}")

    print(f"""
次にやること:
  1. ワールドの datapacks/ に {pack_dir.name} を置く（--install <world>/datapacks で自動化できる）
  2. /reload
  3. /function mcnb:panel     ← 操作盤（コマンドブロック+ボタン）を出してそこへ飛ぶ
  4. 黄緑のボタン = 設置（{result.pack.build_parts} 区画ぶん、少し待つ）
     水色のボタン = 演奏 / 赤 = 停止 / 黄 = 開始位置へ
     コマンドでも同じ: /function mcnb:build /play /stop /goto_start
""")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    tune = song.load_nbs(Path(args.nbs))
    print(song.summarize(tune))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """テスト用 MIDI を生成して全部パイプラインに通し、結果を並べる。"""
    from . import testsongs

    inputs = Path(args.inputs)
    if args.regen or not inputs.is_dir():
        print(f"■ テスト MIDI を生成: {inputs}")
        testsongs.generate_all(inputs)

    names = args.only or list(testsongs.TESTS)
    out_root = Path(args.out)

    header = f"{'テスト':<22}{'tick':>6}{'音':>6}{'同時':>5}{'ブロック':>9}{'長さ(m)':>9}{'音量誤差':>9}{'欠落':>6}"
    print(f"\n{header}\n{'-' * len(header)}")

    failures = 0
    for name in names:
        src = inputs / f"{name}.mid"
        if not src.is_file():
            print(f"{name:<22}  入力なし: {src}")
            failures += 1
            continue
        try:
            r = build_one(src, out_root, name=name, max_polyphony=args.max_polyphony, verbose=False)
        except Exception as e:  # noqa: BLE001 — 1つ落ちても残りは回したい
            print(f"{name:<22}  失敗: {e}")
            failures += 1
            continue

        lay = r.layout
        errors = [abs(p.gain - p.velocity) for p in lay.placements]
        mean_err = sum(errors) / len(errors) if errors else 0.0
        dropped = lay.dropped_polyphony + lay.dropped_unplaceable
        print(
            f"{name:<22}{r.song.length_ticks:>6}{len(r.song.events):>6}"
            f"{r.song.max_polyphony:>5}{lay.block_count:>9}"
            f"{lay.length_blocks:>9}{mean_err:>9.3f}{dropped:>6}"
        )

    print(f"\n出力: {out_root}/<テスト名>/<テスト名>_datapack")
    if failures:
        print(f"失敗: {failures}")
    return 1 if failures else 0


def cmd_setup(args: argparse.Namespace) -> int:
    print("=== Minecraft 音源の抽出 ===")
    rc = mcassets.main(["--out", "assets/mc"] + (["--force"] if args.force else []))
    if rc:
        return rc
    print("\n=== 音源の実測 ===")
    samples.main([])
    print("\n=== Fabric + 軽量化 Mod ===")
    return mcmods.main(["--setup"])


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(prog="mcnb", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="音源/MIDI/NBS → データパック")
    p.add_argument("input")
    p.add_argument("--name", default=None)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--origin", type=int, nargs=3, default=[0, 100, 0], metavar=("X", "Y", "Z"))
    p.add_argument("--spacing", type=int, default=layout.SPACING, help="1 tick あたりの X ブロック数")
    p.add_argument("--max-polyphony", type=int, default=200,
                   help="1 tick の最大同時発音（バニラ247 / RSLS導入なら4095まで）")
    p.add_argument("--install", default=None, metavar="DIR",
                   help="書き出したあとこのディレクトリにコピー（<world>/datapacks）")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("info", help="NBS の中身を表示")
    p.add_argument("nbs")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("test", help="テスト曲 1-10 を全部通して結果を並べる")
    p.add_argument("--inputs", default="tests/inputs")
    p.add_argument("--out", default="out/tests")
    p.add_argument("--only", nargs="*", default=None)
    p.add_argument("--regen", action="store_true", help="テスト MIDI を作り直す")
    p.add_argument("--max-polyphony", type=int, default=200)
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("setup", help="音源抽出 + Fabric + 軽量化 Mod")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_setup)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
