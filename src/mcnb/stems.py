"""音源を声と伴奏に分ける。

歌ものでは、**声と伴奏を分けないと解析がどちらにも失敗する**。

- コード推定は伴奏で決まる。声は和音に属さない音（経過音・こぶし・息）を
  たっぷり持ち込むので、混ざったままだと和音がぼやける
- 主旋律はたいてい声そのもの。伴奏ごと見ると、どれが旋律か決まらない

分けてしまえば、どちらも本来見るべきものだけを見られる。
副産物として伴奏だけの音（カラオケ）が手に入る。

    uv run python -m mcnb.stems cache/audio/song.wav

モデルの重みは配布物に含めない（ライセンスが不明瞭なものが多い）。
初回だけ自動で落ちてきて ``models/separator/`` に入る。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

#: 既定のモデル。BS-RoFormer は声/伴奏の 2 分割で今のところ最良の部類。
#: hyperchoron も同じものを使うので、重みを二重に持たずに済む。
DEFAULT_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
#: 重みの置き場。`models/` は .gitignore 済み
MODEL_DIR = Path("models/separator")
#: 分けた結果の置き場
CACHE_DIR = Path("cache/stems")


class StemError(RuntimeError):
    pass


@dataclass(frozen=True)
class Stems:
    """分けた結果。"""

    vocals: Path
    instrumental: Path
    source: Path
    cached: bool = False

    def exists(self) -> bool:
        return self.vocals.is_file() and self.instrumental.is_file()


def _ensure_ffmpeg() -> None:
    """audio-separator は PATH 上に ffmpeg という名前があることを要求する。"""
    if shutil.which("ffmpeg"):
        return
    try:
        from . import fetch

        fetch.ensure_ffmpeg_on_path()
    except (ImportError, AttributeError, RuntimeError) as e:
        raise StemError("ffmpeg が見つかりません: uv sync --extra audio") from e


def separate(
    source: Path | str,
    out_dir: Path | None = None,
    model: str = DEFAULT_MODEL,
    force: bool = False,
    verbose: bool = True,
) -> Stems:
    """声と伴奏に分ける。すでに分けてあれば作り直さない。"""
    source = Path(source)
    if not source.is_file():
        raise StemError(f"音源が見つかりません: {source}")

    out_dir = Path(out_dir) if out_dir else CACHE_DIR / source.stem
    result = Stems(
        vocals=out_dir / "vocals.wav",
        instrumental=out_dir / "instrumental.wav",
        source=source,
        cached=True,
    )
    if result.exists() and not force:
        if verbose:
            print(f"  分離済み: {out_dir}")
        return result

    try:
        from audio_separator.separator import Separator
    except ImportError as e:
        raise StemError("音源分離の依存が入っていません: uv sync --extra audio") from e

    _ensure_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"  分離中… ({model})")
        print("  初回はモデルの重みを落とすので時間がかかります")

    separator = Separator(
        log_level=40,                     # ERROR。進行はこちらで出す
        model_file_dir=str(MODEL_DIR),
        output_dir=str(out_dir),
        output_format="WAV",
    )
    separator.load_model(model_filename=model)
    # 名前を固定しておく。既定だとモデル名がファイル名に入って扱いにくい
    separator.separate(str(source), custom_output_names={"Vocals": "vocals", "Instrumental": "instrumental"})

    if not result.exists():
        produced = sorted(p.name for p in out_dir.glob("*.wav"))
        raise StemError(f"分離結果が見つかりません。出たもの: {produced or 'なし'}")
    return Stems(vocals=result.vocals, instrumental=result.instrumental, source=source, cached=False)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="音源を声と伴奏に分ける")
    ap.add_argument("audio")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--force", action="store_true", help="分離済みでもやり直す")
    args = ap.parse_args(argv)

    try:
        stems = separate(args.audio, args.out, model=args.model, force=args.force)
    except StemError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    print(f"  声      : {stems.vocals}")
    print(f"  伴奏    : {stems.instrumental}  ← カラオケ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
