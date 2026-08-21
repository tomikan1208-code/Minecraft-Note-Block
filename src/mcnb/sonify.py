"""解析結果を音にして、耳で確かめられるようにする。

コード進行や主旋律が合っているかは、**聴けば一発で分かるが数字では分からない**。
`musical.analyze()` の結果を音に戻し、原音に重ねて書き出す。

    uv run python -m mcnb.sonify cache/audio/song.wav --out out/analysis

出るもの::

    beats.wav    原音（小さめ）＋ 拍のクリック。小節頭は高い音
    chords.wav   原音（小さめ）＋ 推定コードのパッド
    melody.wav   原音（小さめ）＋ 抽出した主旋律の単音
    melody_solo.wav  主旋律だけ
    karaoke.wav  伴奏だけ（声を抜いたもの）。分離できたときだけ

原音を小さく混ぜてあるのは、**ずれていたら即分かる**ようにするため。
単独で聴くと「それらしく」聞こえてしまう。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from .musical import HOP, MusicalContext, analyze

#: 原音を混ぜる音量。小さすぎると照合にならず、大きすぎると重ねた音が埋もれる
ORIGINAL_GAIN = 0.35

#: 何を確かめるためのファイルか
TRACK_LABELS = {
    "beats": "拍とテンポが合っているか（クリックが曲に乗っているか）",
    "chords": "コードが合っているか（パッドが原曲とぶつからないか）",
    "melody": "主旋律が合っているか（原曲の旋律をなぞっているか）",
    "melody_solo": "主旋律だけ",
    "karaoke": "声が抜けているか（伴奏だけ）",
}
#: 立ち上がり・切れ際のなまし（秒）。ぶつ切りのプチノイズを防ぐ
RAMP = 0.005


def _ramp(gate: np.ndarray, sr: int) -> np.ndarray:
    """0/1 のゲートをなまして、境目のプチッという音を消す。"""
    width = max(1, int(RAMP * sr))
    kernel = np.ones(width, dtype=np.float32) / width
    return np.convolve(gate.astype(np.float32), kernel, mode="same")


def sonify_melody(context: MusicalContext, sr: int, n_samples: int) -> np.ndarray:
    """主旋律を単音で鳴らす。

    音の切れ目でも位相を繋いだままにして、周波数だけ動かす。
    ゲートで鳴り止めるので、飛んだところでプチッと鳴らない。
    """
    if not context.melody:
        return np.zeros(n_samples, dtype=np.float32)

    n_frames = n_samples // HOP + 2
    midi = np.full(n_frames, np.nan, dtype=np.float32)
    for t, pitch in context.melody:
        i = int(round(t * sr / HOP))
        if 0 <= i < n_frames:
            midi[i] = pitch

    voiced = np.isfinite(midi)
    if not voiced.any():
        return np.zeros(n_samples, dtype=np.float32)
    # 無声区間は直前の音高を引き延ばす（鳴らさないので聞こえないが、位相が暴れない）
    idx = np.where(voiced, np.arange(n_frames), 0)
    np.maximum.accumulate(idx, out=idx)
    midi = midi[idx]
    midi[: np.argmax(voiced)] = midi[np.argmax(voiced)]

    frame_times = np.arange(n_frames) * HOP / sr
    times = np.arange(n_samples) / sr
    freq = 440.0 * 2.0 ** ((np.interp(times, frame_times, midi) - 69.0) / 12.0)
    gate = _ramp(np.interp(times, frame_times, voiced.astype(np.float32)) > 0.5, sr)

    phase = np.cumsum(2.0 * np.pi * freq / sr)
    # 基音だけだと混ざったとき埋もれるので、倍音を少し足して輪郭を出す
    wave = np.sin(phase) + 0.35 * np.sin(2.0 * phase) + 0.15 * np.sin(3.0 * phase)
    return (wave * gate * 0.28).astype(np.float32)


def sonify_chords(context: MusicalContext, sr: int, n_samples: int) -> np.ndarray:
    """推定したコードをパッドで鳴らす。"""
    out = np.zeros(n_samples, dtype=np.float32)
    for chord in context.chords:
        if chord.root < 0:
            continue
        start = int(chord.start * sr)
        end = min(int(chord.end * sr), n_samples)
        if end - start < 32:
            continue
        length = end - start
        t = np.arange(length) / sr
        envelope = _ramp(np.ones(length, dtype=np.float32), sr)

        # 構成音は C4 まわりに置き、根音だけ 1 オクターブ下にも重ねる
        voices = [48 + pc for pc in chord.pitch_classes]
        voices.append(36 + chord.root)
        segment = np.zeros(length, dtype=np.float32)
        for midi in voices:
            freq = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
            segment += np.sin(2.0 * np.pi * freq * t)
        out[start:end] += segment / len(voices) * envelope * 0.30
    return out


def sonify_beats(context: MusicalContext, sr: int, n_samples: int) -> np.ndarray:
    """拍にクリックを置く。小節頭は高い音にする。"""
    out = np.zeros(n_samples, dtype=np.float32)
    downbeats = set(round(t, 4) for t in context.downbeats)
    length = int(0.04 * sr)
    t = np.arange(length) / sr
    decay = np.exp(-t * 90.0)
    for beat in context.beats:
        start = int(beat * sr)
        end = min(start + length, n_samples)
        if end <= start:
            continue
        high = round(beat, 4) in downbeats
        freq = 1600.0 if high else 900.0
        gain = 0.45 if high else 0.25
        out[start:end] += (np.sin(2.0 * np.pi * freq * t) * decay)[: end - start] * gain
    return out


def _mix(original: np.ndarray, overlay: np.ndarray) -> np.ndarray:
    mixed = original * ORIGINAL_GAIN + overlay
    peak = float(np.max(np.abs(mixed)))
    return mixed / peak * 0.97 if peak > 0.97 else mixed


def write_all(
    audio: Path | str,
    out_dir: Path,
    context: MusicalContext | None = None,
    duration: float | None = None,
) -> dict[str, Path]:
    """解析して、確かめ用の wav を一式書き出す。"""
    import librosa
    import soundfile as sf

    audio = Path(audio)
    if context is None:
        context = analyze(audio, duration=duration, verbose=True)

    y, sr = librosa.load(str(audio), sr=None, mono=True, duration=duration)
    n = len(y)

    tracks = {
        "beats": _mix(y, sonify_beats(context, sr, n)),
        "chords": _mix(y, sonify_chords(context, sr, n)),
        "melody": _mix(y, sonify_melody(context, sr, n)),
        "melody_solo": sonify_melody(context, sr, n) * 3.0,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, wave in tracks.items():
        path = out_dir / f"{name}.wav"
        sf.write(path, np.clip(wave, -1.0, 1.0), sr)
        written[name] = path

    # 伴奏だけの音は分離の副産物。声を抜いた結果が妥当かは、これを聴けば分かる
    if context.instrumental and Path(context.instrumental).is_file():
        karaoke, ksr = librosa.load(context.instrumental, sr=None, mono=False, duration=duration)
        path = out_dir / "karaoke.wav"
        sf.write(path, karaoke.T if karaoke.ndim > 1 else karaoke, ksr)
        written["karaoke"] = path

    return written


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="解析結果を音にして耳で確かめる")
    ap.add_argument("audio")
    ap.add_argument("--out", type=Path, default=Path("out/analysis"))
    ap.add_argument("--duration", type=float, default=None, help="先頭の秒数だけ")
    args = ap.parse_args(argv)

    context = analyze(args.audio, duration=args.duration, verbose=True)
    print()
    print(context.summary())
    print()

    written = write_all(args.audio, args.out, context=context, duration=args.duration)
    print("聴いて確かめてください:")
    for name, path in written.items():
        print(f"  {path}  … {TRACK_LABELS.get(name, name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
