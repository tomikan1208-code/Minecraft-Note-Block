"""抽出した音符ブロック音源の実測値。

音符ブロックの音はワンショットで、サステインが無い。長い音符は再発音するしかないので、
**その再発音間隔を決めるには各サンプルが実際どれだけで減衰しきるかが要る**。
ここではその減衰時間を測る。

    uv run python -m mcnb.samples
    uv run python -m mcnb.samples --assets assets/mc --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .mcassets import AssetError, load_manifest

# 減衰を測る基準。-40 dB はほぼ聞こえなくなる点、-20 dB は明らかに減った点。
DECAY_LEVELS_DB = (-20.0, -40.0)
# 包絡を取るときの移動平均窓（秒）。5 ms あれば波形のリップルは均せる。
ENVELOPE_WINDOW_S = 0.005


@dataclass(frozen=True)
class SampleStats:
    instrument: str
    file: str
    duration: float
    samplerate: int
    channels: int
    peak: float
    #: ピークから n dB 落ちるまでの時間（秒）。キーは "-20" のような文字列
    decay: dict[str, float]

    @property
    def effective_length(self) -> float:
        """実質的に鳴っている長さ（-40 dB まで）。再発音間隔の目安。"""
        return self.decay.get("-40", self.duration)


def _envelope(mono: np.ndarray, samplerate: int) -> np.ndarray:
    window = max(1, int(samplerate * ENVELOPE_WINDOW_S))
    kernel = np.ones(window, dtype=np.float32) / window
    return np.convolve(np.abs(mono), kernel, mode="same")


def analyze_file(path: Path, instrument: str = "") -> SampleStats:
    audio, samplerate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    peak = float(np.abs(mono).max())

    decay: dict[str, float] = {}
    if peak > 0:
        env = _envelope(mono, samplerate)
        for db in DECAY_LEVELS_DB:
            threshold = peak * 10 ** (db / 20)
            above = np.nonzero(env > threshold)[0]
            decay[f"{db:g}"] = float(above[-1] / samplerate) if len(above) else 0.0
    else:
        decay = {f"{db:g}": 0.0 for db in DECAY_LEVELS_DB}

    return SampleStats(
        instrument=instrument or path.stem,
        file=path.name,
        duration=len(mono) / samplerate,
        samplerate=samplerate,
        channels=audio.shape[1],
        peak=peak,
        decay=decay,
    )


def analyze_assets(assets_dir: Path, include_imitate: bool = False) -> list[SampleStats]:
    """抽出済みディレクトリ内の全サンプルを測る。"""
    manifest = load_manifest(assets_dir)
    stats: list[SampleStats] = []
    for sample in manifest.samples:
        if sample.imitate and not include_imitate:
            continue
        for i, f in enumerate(sample.files):
            label = sample.instrument if len(sample.files) == 1 else f"{sample.instrument}[{i}]"
            stats.append(analyze_file(assets_dir / f.file, label))
    return stats


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="音符ブロック音源の長さと減衰時間を測る")
    ap.add_argument("--assets", type=Path, default=Path("assets/mc"))
    ap.add_argument("--imitate", action="store_true", help="Mob ヘッド音も含める")
    ap.add_argument("--json", action="store_true", help="JSON で出力")
    args = ap.parse_args(argv)

    try:
        stats = analyze_assets(args.assets, include_imitate=args.imitate)
    except AssetError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps([asdict(s) for s in stats], indent=2, ensure_ascii=False))
        return 0

    print(f"{'instrument':22s} {'長さ':>7s} {'-20dB':>7s} {'-40dB':>7s} {'sr':>6s} {'ch':>3s}")
    print("-" * 58)
    for s in stats:
        print(
            f"{s.instrument:22s} {s.duration:7.3f} {s.decay['-20']:7.3f} "
            f"{s.decay['-40']:7.3f} {s.samplerate:6d} {s.channels:3d}"
        )

    pitched = [s for s in stats if s.instrument not in ("snare", "hat", "basedrum")]
    if pitched:
        longest = max(pitched, key=lambda s: s.effective_length)
        print(
            f"\n有音程で最も長く残るのは {longest.instrument} "
            f"({longest.effective_length:.3f} 秒 = {longest.effective_length * 20:.1f} tick)。"
        )
        print("サステインの再発音間隔はこの値を上限に決める。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
