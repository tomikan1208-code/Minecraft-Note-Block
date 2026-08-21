"""mcnb の CLI。

    uv run mcnb setup                      # Fabric + 軽量化 Mod + 音源抽出
    uv run mcnb build song.mp3             # 音源 → データパック
    uv run mcnb build song.mid --name test
    uv run mcnb info out/song/song.nbs     # NBS の中身を見る
"""

from __future__ import annotations

import argparse
import os
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

    # audio-separator は PATH 上の ffmpeg を要求する。imageio-ffmpeg の同梱を通す
    from . import fetch as fetch_mod

    env = dict(os.environ)
    bin_dir = fetch_mod.ensure_ffmpeg_on_path()
    if bin_dir:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
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


def resolve_input(source: str, cache: Path = Path("cache/audio")) -> tuple[Path, str | None]:
    """入力が URL なら落としてローカルのパスにする。``(path, タイトル)`` を返す。"""
    from . import fetch as fetch_mod

    if not fetch_mod.is_url(source):
        return Path(source), None

    print(f"■ URL から取得: {source}")
    media = fetch_mod.fetch(source, cache)
    print()
    print(f"  {media.title} / {media.uploader}")
    print(f"  {int(media.duration) // 60}:{int(media.duration) % 60:02d}"
          + ("  (キャッシュ済み)" if media.cached else ""))
    return media.path, media.slug


def build_one(
    src: Path,
    out_root: Path,
    name: str | None = None,
    origin: tuple[int, int, int] = (0, layout.FLAT_SURFACE_Y, 0),
    spacing: int = layout.SPACING,
    max_polyphony: int = 200,
    verbose: bool = True,
    arrange_config=None,
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

    if arrange_config is not None:
        from . import arrange as arrange_mod
        tune, arrange_config = arrange_mod.arrange(tune, arrange_config)
        if verbose:
            print("\n■ 編曲")
            print(arrange_mod.summarize(arrange_config))

    lay = layout.build_layout(tune, origin=origin, spacing=spacing, max_polyphony=max_polyphony)
    if verbose:
        print("\n■ Minecraft 空間に配置（直線コリドー）")
        print(layout.summarize(lay))

    pack = datapack.emit(lay, out / f"{name}_datapack", name=name)
    if verbose:
        print("\n■ データパック")
        print(datapack.summarize(pack))

    return BuildResult(name=name, song=tune, layout=lay, pack=pack, nbs=nbs_path)


def _arrange_config(args: argparse.Namespace):
    """``--concurrent`` が指定されたときだけ編曲を掛ける。"""
    if not getattr(args, "concurrent", 0):
        return None
    from . import arrange as arrange_mod

    return arrange_mod.ArrangeConfig(max_concurrent=args.concurrent)


def cmd_build(args: argparse.Namespace) -> int:
    try:
        src, fetched_name = resolve_input(args.input)
    except Exception as e:  # noqa: BLE001
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if not src.is_file():
        print(f"エラー: {src} がありません", file=sys.stderr)
        return 1

    print(f"■ 入力: {src}")
    try:
        result = build_one(
            src,
            Path(args.out),
            name=args.name or fetched_name,
            origin=tuple(args.origin),
            spacing=args.spacing,
            max_polyphony=args.max_polyphony,
            arrange_config=_arrange_config(args),
        )
    except (ValueError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    pack_dir = result.pack.path

    if args.world:
        from . import world as world_mod
        r = world_mod.create_world(args.world, datapack=pack_dir, overwrite=True)
        print(f"\n■ 専用ワールド: {r.path}")
        print(f"  ランチャーで「mcnb (音ブロック)」を起動 → ワールド「{r.name}」を開く")

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


def cmd_verify(args: argparse.Namespace) -> int:
    """headless サーバで実際に動かして検証する。"""
    from . import verify

    src = Path(args.input)
    if not src.is_file():
        print(f"エラー: {src} がありません", file=sys.stderr)
        return 1

    print(f"■ ビルド: {src}")
    r = build_one(src, Path(args.out), name=args.name, max_polyphony=args.max_polyphony, verbose=False)
    print(f"  {len(r.layout.placements)} 音 / {r.pack.build_parts} 区画 / {r.pack.commands} コマンド")

    print("\n■ サーバで検証")
    try:
        result = verify.verify_layout(
            r.layout, r.pack.path, sample=args.sample, memory=args.memory
        )
    except verify.ServerError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    print(verify.format_result(result))
    return 0 if result.ok else 1


def cmd_render(args: argparse.Namespace) -> int:
    """配置を音にする。Minecraft を起動せずに「こう聞こえるはず」を作る。"""
    from . import render

    try:
        src, _ = resolve_input(args.input)
        r = build_one(src, Path(args.out), name=args.name,
                      max_polyphony=args.max_polyphony, verbose=False,
                      arrange_config=_arrange_config(args))

        model = render.AcousticModel(polyphony=args.polyphony)
        if args.measurements and Path(args.measurements).is_file():
            model = render.AcousticModel.from_measurements(Path(args.measurements))
            model.polyphony = args.polyphony
            print(f"■ 実測から較正: {args.measurements}")
            print(f"  減衰カーブ {model.curve} / 可聴距離 {model.max_hearing:g} ブロック")
        else:
            print("■ 音響モデル: 未較正（線形 1 − d/48 と仮定）")
            print("  `mcnb measure` を回すと実測で置き換わります")

        print(f"\n■ 合成中… {len(r.layout.placements)} 音")
        result = render.render_layout(r.layout, model=model)
        wav = render.save(result, Path(r.pack.path).parent / f"{r.name}_minecraft.wav")
        print(render.summary_indent(result))
        print(f"\n  {wav}")

        if args.compare and src.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
            print("\n■ 原曲との距離")
            print(render.format_compare(render.compare(src, wav)))

        if args.plot:
            png = render.plot_compare(wav, src if src.suffix.lower() in {'.wav', '.mp3', '.flac', '.m4a'} else None,
                                      Path(r.pack.path).parent / f"{r.name}_spectrogram.png")
            print(f"  {png}")
    except (render.RenderError, ValueError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """1画面から全部やる GUI を立てる。"""
    from . import gui

    gui.serve(args.host, args.port, open_browser=not args.no_browser)
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    """実機測定リグを回す。クライアントの接続が要る。"""
    from . import audio, measure

    if args.selftest:
        ok, message = audio.selftest()
        print(("OK  " if ok else "NG  ") + message)
        return 0 if ok else 1

    try:
        report = measure.run(
            only=args.only, out_dir=Path(args.out), memory=args.memory, wait=args.wait
        )
    except (measure.ServerError, audio.AudioError, ValueError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if "distance" in report.sections:
        print("\n■ 距離減衰の判定")
        for line in measure.analyze_distance(report.sections["distance"]).splitlines():
            print("  " + line)
    return 0


def cmd_world(args: argparse.Namespace) -> int:
    """演奏専用ワールドを新規生成する。"""
    from . import world as world_mod

    pack = Path(args.datapack) if args.datapack else None
    try:
        r = world_mod.create_world(
            args.name, datapack=pack, overwrite=args.overwrite, memory=args.memory
        )
    except Exception as e:  # noqa: BLE001
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if r.created:
        print(f"■ ワールドを作成: {r.path}")
        print("  チートON / クリエイティブ / フラット地形 / 生成物なし / モブ湧きなし")
    else:
        print(f"■ 既にあります: {r.path}（--overwrite で作り直し）")
    if pack:
        print(f"  データパック: {pack.name}")
    print(f"\nMinecraft ランチャーで「mcnb (音ブロック)」を起動 → ワールド「{r.name}」を開く")
    return 0


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
    p.add_argument("input", help="音源ファイル / MIDI / NBS / YouTube などの URL")
    p.add_argument("--name", default=None)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--origin", type=int, nargs=3, default=[0, layout.FLAT_SURFACE_Y, 0],
                   metavar=("X", "Y", "Z"), help="既定はフラット地形の地表 (0, -60, 0)")
    p.add_argument("--spacing", type=int, default=layout.SPACING, help="1 tick あたりの X ブロック数")
    p.add_argument("--max-polyphony", type=int, default=200,
                   help="1 tick の最大同時発音（バニラ247 / RSLS導入なら4095まで）")
    p.add_argument("--concurrent", type=int, default=0, metavar="N",
                   help="編曲: 同時に鳴っている音を N 個までに抑える（0 で編曲なし）")
    p.add_argument("--install", default=None, metavar="DIR",
                   help="書き出したあとこのディレクトリにコピー（<world>/datapacks）")
    p.add_argument("--world", default=None, metavar="NAME",
                   help="演奏専用ワールドを作ってデータパックまで入れる")
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

    p = sub.add_parser("world", help="演奏専用ワールドを新規生成する（チートON/クリエイティブ/フラット）")
    p.add_argument("--name", default="mcnb")
    p.add_argument("--datapack", default=None, help="同時に入れるデータパックのディレクトリ")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--memory", default="2G")
    p.set_defaults(func=cmd_world)

    p = sub.add_parser("render", help="配置を音にする（Minecraft 不要）")
    p.add_argument("input")
    p.add_argument("--name", default=None)
    p.add_argument("--out", default="out")
    p.add_argument("--max-polyphony", type=int, default=200, help="配置時の同時発音上限")
    p.add_argument("--concurrent", type=int, default=0, metavar="N",
                   help="編曲: 同時に鳴っている音を N 個までに抑える（0 で編曲なし）")
    p.add_argument("--polyphony", type=int, default=247, help="再生時の上限（バニラ247 / RSLS4095）")
    p.add_argument("--measurements", default="out/measure/measurements.json",
                   help="実測から音響モデルを較正する")
    p.add_argument("--plot", action="store_true", help="スペクトログラムを並べた図も出す")
    p.add_argument("--compare", action="store_true", help="原曲との距離を数値で出す（入力が音源のとき）")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("gui", help="1画面から全部やる GUI（ブラウザ）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("measure", help="実機測定リグ（要クライアント接続）")
    p.add_argument("--only", nargs="*", default=None,
                   help="distance / instruments / pitch / panning")
    p.add_argument("--out", default="out/measure")
    p.add_argument("--memory", default="2G")
    p.add_argument("--wait", type=float, default=600.0, help="クライアント接続を待つ秒数")
    p.add_argument("--selftest", action="store_true", help="録音経路だけ確認して終わる")
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("verify", help="headless サーバで実際に動かして検証する")
    p.add_argument("input")
    p.add_argument("--name", default=None)
    p.add_argument("--out", default="out/verify")
    p.add_argument("--sample", type=int, default=24, help="ブロックを確認する箇所の数")
    p.add_argument("--max-polyphony", type=int, default=200)
    p.add_argument("--memory", default="2G")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("setup", help="音源抽出 + Fabric + 軽量化 Mod")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_setup)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
