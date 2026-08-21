"""mcnb の CLI。

    uv run mcnb setup                      # Fabric + 軽量化 Mod + 音源抽出
    uv run mcnb build song.mp3             # 音源 → データパック
    uv run mcnb build song.mid --name test
    uv run mcnb info out/song/song.nbs     # NBS の中身を見る
"""

from __future__ import annotations

import argparse
import json
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

    NBS ヘッダーの曲名は pynbs が cp1252 でエンコードするため、
    日本語などの非 ASCII 名を直接書き込めない。ASCII セーフな作業名で
    hyperchoron を実行し、出力後に希望のファイル名へ renaming する。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 非 ASCII ファイル名 → 一時名で変換して後から updates
    work_dest = dest
    if any(ord(c) > 127 for c in dest.stem):
        tmp = dest.with_name(f"_mcnb_tmp_{abs(hash(dest)) % 0xFFFF}{dest.suffix}")
        work_dest = tmp

    cmd = [
        sys.executable, "-m", "hyperchoron",
        "-i", str(src),
        "-o", str(work_dest),
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
    if proc.returncode != 0 or not work_dest.is_file():
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
        raise RuntimeError(f"hyperchoron が失敗しました (exit {proc.returncode})")

    if work_dest != dest:
        shutil.move(str(work_dest), str(dest))


@dataclass
class BuildResult:
    name: str
    song: song.Song
    layout: layout.Layout
    pack: datapack.DatapackResult
    nbs: Path


def _safe_name(title: str) -> str:
    """曲名をファイル名に使える形にする。"""
    cleaned = "".join(c if c.isalnum() or c in "ー〜_-" else "_" for c in title.strip())
    return cleaned.strip("_")[:60] or "score"


def resolve_input(source: str, cache: Path = Path("cache/audio")) -> tuple[Path, str | None]:
    """入力が URL なら落としてローカルのパスにする。``(path, タイトル)`` を返す。"""
    from . import fetch as fetch_mod

    if not fetch_mod.is_url(source):
        return Path(source), None

    if fetch_mod.is_flat_url(source):
        print(f"■ flat.io から楽譜を取得: {source}")
        path, title = fetch_mod.fetch_flat_score(source)
        print(f"  {title}")
        return path, _safe_name(title)

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
    speed: float | None = None,
    max_polyphony: int = 200,
    verbose: bool = True,
    arrange_config=None,
    move: str = "vehicle",
    refresh: bool = False,
    source: str = "mix",
    blocks_per_note: int = 1,
    transcriber: str = "hyperchoron",
) -> BuildResult:
    """1つの入力を最後まで通す。CLI からもテストランナーからも使う。"""
    name = name or src.stem
    out = out_root / name
    out.mkdir(parents=True, exist_ok=True)

    # 採譜に使う音を選ぶ。既定は原音（ボーカル込み）。
    # instrumental を選ぶと、声を抜いた伴奏だけを音符にする
    original = src
    if source == "instrumental" and src.suffix.lower() in AUDIO_SUFFIXES:
        from . import stems as stems_mod

        if verbose:
            print("\n■ 声と伴奏に分ける（伴奏だけを採譜する）")
        try:
            src = stems_mod.separate(src, verbose=verbose).instrumental
        except stems_mod.StemError as e:
            print(f"  分離できませんでした（原音のまま進みます）: {e}", file=sys.stderr)

    suffix = "" if source == "mix" else f"_{source}"
    nbs_path = out / f"{name}{suffix}.nbs"

    if src.suffix.lower() in SCORE_SUFFIXES:
        # 楽譜はそのまま置く。採譜を通さないので、音を足しも引きもしない
        from . import score as score_mod

        if verbose:
            print("\n■ 楽譜をそのまま音符ブロックへ")
        sheet = score_mod.read_score(src)
        if verbose:
            print(score_mod.summary_or_empty(sheet))
        tune = score_mod.to_song(sheet, name=name, blocks_per_note=blocks_per_note)
        nbs_path = out / f"{name}.nbs"
    elif src.suffix.lower() == ".nbs":
        if nbs_path.resolve() != src.resolve():
            shutil.copy2(src, nbs_path)
    elif transcriber in ("basic-pitch", "piano") and src.suffix.lower() in AUDIO_SUFFIXES:
        # 音符に起こしてから置く。CQT のピーク拾いと違い、1 音ずつに意味がある
        from . import transcribe as transcribe_mod

        if verbose:
            print(f"\n■ 音符に起こす（{transcriber}）")
        notes = (
            transcribe_mod.transcribe_piano(src, verbose=verbose)
            if transcriber == "piano"
            else transcribe_mod.transcribe(src, verbose=verbose)
        )
        if verbose:
            print(transcribe_mod.summarize(notes))
        tune = transcribe_mod.to_song(notes, name=name, blocks_per_note=blocks_per_note)
        nbs_path = out / f"{name}.nbs"
    elif src.suffix.lower() in HYPERCHORON_INPUTS:
        # 採譜は曲によっては 1 時間を超える。編曲だけ試したいときに毎回やり直すのは
        # 無駄なので、原音より新しい採譜結果が残っていればそれを使う。
        fresh = nbs_path.is_file() and nbs_path.stat().st_mtime >= src.stat().st_mtime
        if fresh and not refresh:
            if verbose:
                print(f"\n■ 採譜済みを使います: {nbs_path}")
                print("  やり直すには --refresh")
        else:
            if verbose:
                print("\n■ hyperchoron で音楽的変換 → .nbs")
            run_hyperchoron(src, nbs_path, quiet=not verbose)
    else:
        raise ValueError(f"未対応の拡張子: {src.suffix}")

    if src.suffix.lower() not in SCORE_SUFFIXES and transcriber == "hyperchoron":
        tune = song.load_nbs(nbs_path, name=name)
    if verbose:
        print("\n■ 演奏データ")
        print(song.summarize(tune))

    if arrange_config is not None:
        from . import arrange as arrange_mod

        # 原音があるなら解析して渡す。どの音を残すかを音量ではなく
        # 音楽的な役割（主旋律・コードの構成音・低音）で決められるようになる
        if arrange_config.context is None and original.suffix.lower() in AUDIO_SUFFIXES:
            from . import musical as musical_mod

            if verbose:
                print("\n■ 原音を読み解く（拍・調・コード・主旋律）")
            arrange_config.context = musical_mod.analyze_cached(original, verbose=verbose)
            if verbose:
                print(arrange_config.context.summary())

            # 採譜の時間軸は原音とずれる。ずれたまま突き合わせると、別の時刻の音を
            # 主旋律とみなして大きくしてしまう（実測で -93ms 〜 +372ms）
            offset, score = musical_mod.estimate_offset(
                [(e.tick / 20.0, e.midi, e.velocity) for e in tune.events], original
            )
            arrange_config.time_offset = offset
            if verbose:
                print(f"  採譜と原音のずれ: {offset * 1000:+.0f} ms （一致度 {score:.2f}）")

        tune, arrange_config = arrange_mod.arrange(tune, arrange_config)
        if verbose:
            print("\n■ 編曲")
            print(arrange_mod.summarize(arrange_config))

    lay = layout.build_layout(tune, origin=origin, speed=speed, max_polyphony=max_polyphony)
    if verbose:
        print("\n■ Minecraft 空間に配置（直線コリドー）")
        print(layout.summarize(lay))

    pack = datapack.emit(lay, out / f"{name}_datapack", name=name, move=move)
    if verbose:
        print("\n■ データパック")
        print(datapack.summarize(pack))

    return BuildResult(name=name, song=tune, layout=lay, pack=pack, nbs=nbs_path)


#: 同時に鳴らす音の上限の既定値。生成のたびに --concurrent で変えられる。
#: 上限を外す（--concurrent 0）と、音楽的な取捨だけが効いて重なりは制限しない。
DEFAULT_CONCURRENT = 60


def _arrange_config(args: argparse.Namespace):
    """編曲の設定を作る。

    以前は ``--concurrent`` を指定したときだけ編曲していたが、そうすると
    **既定では音色の割り当ても強弱も効かない**。編曲は常に掛けて、
    重なりの上限だけを指定で変えられるようにする。``--no-arrange`` で全部切れる。
    """
    from . import arrange as arrange_mod

    sparkle = getattr(args, "sparkle", False)
    extras = sparkle or getattr(args, "reverb", False) or getattr(args, "underpin", False)
    if getattr(args, "no_arrange", False):
        if not extras:
            return None
        # 「編曲なし」でも重ねだけは掛けたい。音を消さないので、
        # 間引きの良し悪しとは独立に効く
        return arrange_mod.ArrangeConfig(
            sparkle=sparkle, underpin=getattr(args, "underpin", False),
            underpin_gain=getattr(args, "underpin_gain", arrange_mod.UNDERPIN_GAIN),
            reverb=getattr(args, "reverb", False),
            reverb_gain=getattr(args, "reverb_gain", 1.0), quantize=False, voice_roles=False, emphasize_melody=False,
            fold_harmonics=False, dedupe=False, thin_sustains=False, max_concurrent=0,
            sparkle_voices=getattr(args, "sparkle_voices", 1),
            sparkle_gain=getattr(args, "sparkle_gain", arrange_mod.SPARKLE_GAIN),
        )
    return arrange_mod.ArrangeConfig(
        sparkle=sparkle,
        underpin=getattr(args, "underpin", False),
        underpin_gain=getattr(args, "underpin_gain", arrange_mod.UNDERPIN_GAIN),
        reverb=getattr(args, "reverb", False),
        reverb_gain=getattr(args, "reverb_gain", 1.0),
        sparkle_voices=getattr(args, "sparkle_voices", 1),
        sparkle_gain=getattr(args, "sparkle_gain", arrange_mod.SPARKLE_GAIN),
        max_concurrent=getattr(args, "concurrent", DEFAULT_CONCURRENT),
        quantize=not getattr(args, "no_quantize", False),
        division=getattr(args, "division", 4) or 4,
        voice_roles=not getattr(args, "no_voice_roles", False),
        emphasize_melody=not getattr(args, "no_emphasis", False),
    )


#: 原音として扱う拡張子
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}
#: 楽譜として扱う拡張子。採譜を通さずそのまま置く
SCORE_SUFFIXES = {".xml", ".musicxml", ".mxl", ".json"}


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
            speed=args.speed,
            max_polyphony=args.max_polyphony,
            arrange_config=_arrange_config(args),
            move=getattr(args, "move", "vehicle"),
            refresh=getattr(args, "refresh", False),
            source=getattr(args, "source", "mix"),
            blocks_per_note=getattr(args, "blocks_per_note", 1),
            transcriber=getattr(args, "transcriber", "hyperchoron"),
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

    try:
        src, fetched = resolve_input(args.input)
    except (ValueError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    if not src.is_file():
        print(f"エラー: {src} がありません", file=sys.stderr)
        return 1
    if args.name is None:
        args.name = fetched

    print(f"■ ビルド: {src}")
    r = build_one(src, Path(args.out), name=args.name, max_polyphony=args.max_polyphony,
                  verbose=False, arrange_config=_arrange_config(args),
                  refresh=getattr(args, "refresh", False),
                  blocks_per_note=getattr(args, "blocks_per_note", 1),
                  transcriber=getattr(args, "transcriber", "hyperchoron"))
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
                      arrange_config=_arrange_config(args),
                      refresh=getattr(args, "refresh", False),
                      blocks_per_note=getattr(args, "blocks_per_note", 1),
                      transcriber=getattr(args, "transcriber", "hyperchoron"))

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


def cmd_analyze(args: argparse.Namespace) -> int:
    """原音から拍・調・コード進行・主旋律を取り出し、聴いて確かめられる形で出す。"""
    from . import musical, sonify

    try:
        src, _ = resolve_input(args.input)
    except (ValueError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if src.suffix.lower() not in {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"}:
        print(f"エラー: 音源を渡してください（{src.suffix} は解析できません）", file=sys.stderr)
        return 1

    out = Path(args.out) / (args.name or src.stem)
    print(f"■ 解析: {src.name}")
    context = musical.analyze(src, duration=args.duration, melody=not args.no_melody,
                              stems=not args.no_stems, verbose=True)
    print()
    print(context.summary())

    out.mkdir(parents=True, exist_ok=True)
    (out / "analysis.json").write_text(
        json.dumps(context.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if args.no_sonify:
        print(f"\n  {out / 'analysis.json'}")
        return 0

    print("\n■ 音にしています…")
    written = sonify.write_all(src, out, context=context, duration=args.duration)
    labels = sonify.TRACK_LABELS
    print("\n聴いて確かめてください:")
    for name, path in written.items():
        print(f"  {path}  … {labels.get(name, name)}")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """1画面から全部やる GUI を立てる。"""
    from . import gui

    gui.serve(args.host, args.port, open_browser=not args.no_browser,
              restart=not args.keep_stale)
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    """実機測定リグを回す。クライアントの接続が要る。"""
    from . import audio, measure

    if args.selftest:
        ok, message = audio.selftest()
        print(("OK  " if ok else "NG  ") + message)
        return 0 if ok else 1

    from . import procs
    procs.cleanup(ports=[procs.SERVER_PORT, procs.RCON_PORT])

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
    p.add_argument("--move", choices=datapack.MOVE_MODES, default="vehicle",
                   help="移動のやり方。vehicle=見えない台に乗って動く（既定・滑らか）/ "
                        "teleport=プレイヤーを直接 tp（20fps でカクつく）/ "
                        "run=自分の足で走る（アイテム属性で速度を上げる）")
    p.add_argument("--speed", type=float, default=None, metavar="B",
                   help="プレイヤーの移動速度（ブロック/tick）。"
                        "既定は曲の密度から自動。0.365 = スプリント+速度II")
    p.add_argument("--max-polyphony", type=int, default=200,
                   help="1 tick の最大同時発音（バニラ247 / RSLS導入なら4095まで）")
    p.add_argument("--concurrent", type=int, default=DEFAULT_CONCURRENT, metavar="N",
                   help=f"同時に鳴っている音を N 個までに抑える（既定 {DEFAULT_CONCURRENT} / 0 で無制限）")
    p.add_argument("--no-arrange", action="store_true", help="編曲を一切掛けない（採譜そのまま）")
    p.add_argument("--sparkle", action="store_true",
                   help="最上声部の1オクターブ上を明るい楽器(chime/bell)で重ねる。音は消さない")
    p.add_argument("--underpin", action="store_true",
                   help="いちばん低い音の1オクターブ下を bass で足す。50〜150Hz が -20dB 足りない")
    p.add_argument("--underpin-gain", type=float, default=0.85, metavar="G",
                   help="足す低音の音量（既定 0.85）")
    p.add_argument("--reverb", action="store_true",
                   help="遅らせて小さく鳴らし直し、余韻を作る（音符ブロックにエフェクトは無い）")
    p.add_argument("--reverb-gain", type=float, default=1.0, metavar="G",
                   help="残響の強さ（既定 1.0）")
    p.add_argument("--sparkle-voices", type=int, default=1, metavar="N",
                   help="上から N 声を重ねる（既定 1）")
    p.add_argument("--sparkle-gain", type=float, default=0.55, metavar="G",
                   help="重ねる音の音量。元に対する倍率（既定 0.55）")
    p.add_argument("--no-quantize", action="store_true",
                   help="拍の格子への割り付けをやめる")
    p.add_argument("--division", type=int, default=4, metavar="N",
                   help="1 拍を N 分割した格子に音符を載せる（既定 4 = 16分音符）")
    p.add_argument("--no-voice-roles", action="store_true",
                   help="主旋律・低音への楽器の割り当てをやめる")
    p.add_argument("--no-emphasis", action="store_true",
                   help="主旋律を前に出す（近くに置く）のをやめる")
    p.add_argument("--source", choices=("mix", "instrumental"), default="mix",
                   help="採譜に使う音。mix=原音のまま / instrumental=声を抜いた伴奏だけ")
    p.add_argument("--install", default=None, metavar="DIR",
                   help="書き出したあとこのディレクトリにコピー（<world>/datapacks）")
    p.add_argument("--world", default=None, metavar="NAME",
                   help="演奏専用ワールドを作ってデータパックまで入れる")
    p.add_argument("--refresh", action="store_true", help="採譜をやり直す")
    p.add_argument("--blocks-per-note", type=int, default=1, choices=(1, 2, 3),
                   help="楽譜入力のとき、1 音符に使う音符ブロックの数（2 以上でオクターブを重ねる）")
    p.add_argument("--transcriber", choices=("hyperchoron", "basic-pitch", "piano"),
                   default="hyperchoron",
                   help="音源の採譜のしかた。hyperchoron=CQTのピーク拾い（密）/ "
                        "basic-pitch=音符に起こす（譜面に近い）/ "
                        "piano=ピアノ専用（いちばん正確だがピアノ曲のみ）")
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
    p.add_argument("--concurrent", type=int, default=DEFAULT_CONCURRENT, metavar="N",
                   help=f"同時に鳴っている音を N 個までに抑える（既定 {DEFAULT_CONCURRENT} / 0 で無制限）")
    p.add_argument("--no-arrange", action="store_true", help="編曲を一切掛けない（採譜そのまま）")
    p.add_argument("--sparkle", action="store_true",
                   help="最上声部の1オクターブ上を明るい楽器(chime/bell)で重ねる。音は消さない")
    p.add_argument("--underpin", action="store_true",
                   help="いちばん低い音の1オクターブ下を bass で足す。50〜150Hz が -20dB 足りない")
    p.add_argument("--underpin-gain", type=float, default=0.85, metavar="G",
                   help="足す低音の音量（既定 0.85）")
    p.add_argument("--reverb", action="store_true",
                   help="遅らせて小さく鳴らし直し、余韻を作る（音符ブロックにエフェクトは無い）")
    p.add_argument("--reverb-gain", type=float, default=1.0, metavar="G",
                   help="残響の強さ（既定 1.0）")
    p.add_argument("--sparkle-voices", type=int, default=1, metavar="N",
                   help="上から N 声を重ねる（既定 1）")
    p.add_argument("--sparkle-gain", type=float, default=0.55, metavar="G",
                   help="重ねる音の音量。元に対する倍率（既定 0.55）")
    p.add_argument("--no-quantize", action="store_true",
                   help="拍の格子への割り付けをやめる")
    p.add_argument("--division", type=int, default=4, metavar="N",
                   help="1 拍を N 分割した格子に音符を載せる（既定 4 = 16分音符）")
    p.add_argument("--no-voice-roles", action="store_true",
                   help="主旋律・低音への楽器の割り当てをやめる")
    p.add_argument("--no-emphasis", action="store_true",
                   help="主旋律を前に出す（近くに置く）のをやめる")
    p.add_argument("--source", choices=("mix", "instrumental"), default="mix",
                   help="採譜に使う音。mix=原音のまま / instrumental=声を抜いた伴奏だけ")
    p.add_argument("--polyphony", type=int, default=247, help="再生時の上限（バニラ247 / RSLS4095）")
    p.add_argument("--measurements", default="out/measure/measurements.json",
                   help="実測から音響モデルを較正する")
    p.add_argument("--plot", action="store_true", help="スペクトログラムを並べた図も出す")
    p.add_argument("--compare", action="store_true", help="原曲との距離を数値で出す（入力が音源のとき）")
    p.add_argument("--refresh", action="store_true", help="採譜をやり直す")
    p.add_argument("--blocks-per-note", type=int, default=1, choices=(1, 2, 3),
                   help="楽譜入力のとき、1 音符に使う音符ブロックの数（2 以上でオクターブを重ねる）")
    p.add_argument("--transcriber", choices=("hyperchoron", "basic-pitch", "piano"),
                   default="hyperchoron",
                   help="音源の採譜のしかた。hyperchoron=CQTのピーク拾い（密）/ "
                        "basic-pitch=音符に起こす（譜面に近い）/ "
                        "piano=ピアノ専用（いちばん正確だがピアノ曲のみ）")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("analyze", help="原音から拍・調・コード進行・主旋律を取り出す")
    p.add_argument("input", help="音源ファイル / YouTube URL")
    p.add_argument("--name", default=None)
    p.add_argument("--out", default="out/analysis")
    p.add_argument("--duration", type=float, default=None, help="先頭の秒数だけ解析する")
    p.add_argument("--no-melody", action="store_true", help="主旋律の抽出を省く（速い）")
    p.add_argument("--no-stems", action="store_true", help="声と伴奏に分けずに解析する")
    p.add_argument("--no-sonify", action="store_true", help="確認用 wav を書き出さない")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("gui", help="1画面から全部やる GUI（ブラウザ）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--keep-stale", action="store_true", help="前回の残骸を止めずに起動する")
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
    p.add_argument("--concurrent", type=int, default=DEFAULT_CONCURRENT, metavar="N",
                   help=f"同時に鳴っている音を N 個までに抑える（既定 {DEFAULT_CONCURRENT} / 0 で無制限）")
    p.add_argument("--no-arrange", action="store_true", help="編曲を一切掛けない（採譜そのまま）")
    p.add_argument("--sparkle", action="store_true",
                   help="最上声部の1オクターブ上を明るい楽器(chime/bell)で重ねる。音は消さない")
    p.add_argument("--underpin", action="store_true",
                   help="いちばん低い音の1オクターブ下を bass で足す。50〜150Hz が -20dB 足りない")
    p.add_argument("--underpin-gain", type=float, default=0.85, metavar="G",
                   help="足す低音の音量（既定 0.85）")
    p.add_argument("--reverb", action="store_true",
                   help="遅らせて小さく鳴らし直し、余韻を作る（音符ブロックにエフェクトは無い）")
    p.add_argument("--reverb-gain", type=float, default=1.0, metavar="G",
                   help="残響の強さ（既定 1.0）")
    p.add_argument("--sparkle-voices", type=int, default=1, metavar="N",
                   help="上から N 声を重ねる（既定 1）")
    p.add_argument("--sparkle-gain", type=float, default=0.55, metavar="G",
                   help="重ねる音の音量。元に対する倍率（既定 0.55）")
    p.add_argument("--no-quantize", action="store_true",
                   help="拍の格子への割り付けをやめる")
    p.add_argument("--division", type=int, default=4, metavar="N",
                   help="1 拍を N 分割した格子に音符を載せる（既定 4 = 16分音符）")
    p.add_argument("--no-voice-roles", action="store_true",
                   help="主旋律・低音への楽器の割り当てをやめる")
    p.add_argument("--no-emphasis", action="store_true",
                   help="主旋律を前に出す（近くに置く）のをやめる")
    p.add_argument("--source", choices=("mix", "instrumental"), default="mix",
                   help="採譜に使う音。mix=原音のまま / instrumental=声を抜いた伴奏だけ")
    p.add_argument("--refresh", action="store_true", help="採譜をやり直す")
    p.add_argument("--memory", default="2G")
    p.add_argument("--blocks-per-note", type=int, default=1, choices=(1, 2, 3),
                   help="楽譜入力のとき、1 音符に使う音符ブロックの数（2 以上でオクターブを重ねる）")
    p.add_argument("--transcriber", choices=("hyperchoron", "basic-pitch", "piano"),
                   default="hyperchoron",
                   help="音源の採譜のしかた。hyperchoron=CQTのピーク拾い（密）/ "
                        "basic-pitch=音符に起こす（譜面に近い）/ "
                        "piano=ピアノ専用（いちばん正確だがピアノ曲のみ）")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("setup", help="音源抽出 + Fabric + 軽量化 Mod")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_setup)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
