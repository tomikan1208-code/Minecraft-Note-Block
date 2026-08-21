"""スピーカー出力をそのまま録音する（WASAPI ループバック / Windows）。

Minecraft の音を出すのはクライアントだけで、サーバは無音。だから実機測定は
「クライアントに鳴らさせて、その出力を録る」しかない。

    uv run python -m mcnb.audio --devices     # 使えるデバイスを一覧
    uv run python -m mcnb.audio --test 3      # 3秒録って波形の情報を出す

Windows 以外では動かない。macOS/Linux では BlackHole / PulseAudio monitor などの
仮想デバイスを ``--device`` で指定する形にする（未実装）。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: 測定はサンプルレートを揃えたい。48kHz は MC の音源（44.1/48混在）を包含する
TARGET_RATE = 48000
#: 1回の read で取るフレーム数
CHUNK = 1024
#: 録音開始後、最初のフレームが来るまで待つ上限（秒）
START_TIMEOUT = 5.0


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    channels: int
    rate: int
    is_loopback: bool
    #: 対応する再生デバイス。無音を流してループバックを起こしておくのに使う
    output_index: int | None = None

    def __str__(self) -> str:
        mark = "loopback" if self.is_loopback else "        "
        return f"[{self.index:3d}] {mark} {self.rate:6d}Hz {self.channels}ch  {self.name}"


def _pyaudio():
    if sys.platform != "win32":
        raise AudioError("いまは Windows の WASAPI ループバックにしか対応していません。")
    try:
        import pyaudiowpatch
    except ImportError as e:
        raise AudioError(
            "PyAudioWPatch が入っていません: uv sync --extra measure"
        ) from e
    return pyaudiowpatch


def list_devices() -> list[Device]:
    pa = _pyaudio()
    out: list[Device] = []
    with pa.PyAudio() as p:
        for info in p.get_device_info_generator():
            out.append(
                Device(
                    index=int(info["index"]),
                    name=str(info["name"]),
                    channels=int(info["maxInputChannels"]),
                    rate=int(info["defaultSampleRate"]),
                    is_loopback=bool(info.get("isLoopbackDevice", False)),
                )
            )
    return out


def default_loopback() -> Device:
    """既定の再生デバイスに対応するループバックを探す。"""
    pa = _pyaudio()
    with pa.PyAudio() as p:
        try:
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        except OSError as e:
            raise AudioError("WASAPI が使えません。") from e

        speakers = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if speakers.get("isLoopbackDevice"):
            found = speakers
        else:
            found = None
            for info in p.get_loopback_device_info_generator():
                if speakers["name"] in info["name"]:
                    found = info
                    break
        if found is None:
            raise AudioError(
                f"「{speakers['name']}」のループバックが見つかりません。"
                "--device で明示してください。"
            )
        return Device(
            index=int(found["index"]),
            name=str(found["name"]),
            channels=int(found["maxInputChannels"]),
            rate=int(found["defaultSampleRate"]),
            is_loopback=True,
            output_index=int(speakers["index"]),
        )


class Recorder:
    """録音をバックグラウンドで回し続け、必要な区間だけ切り出す。

    「鳴らす → 録る」を毎回開き直すとデバイスの起動遅延で頭が切れる。
    ずっと回しておいて ``mark()`` で位置を控え、``since()`` で切り出す。
    """

    def __init__(self, device: Device | None = None, rate: int = TARGET_RATE):
        self.device = device or default_loopback()
        self.rate = rate if self.device.rate == 0 else self.device.rate
        self.channels = max(1, self.device.channels)
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream = None
        self._pa = None
        self._silence = None
        self._silence_thread: threading.Thread | None = None

    # -- 開始 / 停止 ------------------------------------------------------ #

    def __enter__(self) -> Recorder:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        pa = _pyaudio()
        self._pa = pa.PyAudio()

        # WASAPI のループバックは「何も再生されていないとフレームを1つも返さず
        # read() が永久にブロックする」。無音を流し続けてレンダラを起こしておく。
        self._start_keepalive(pa)

        self._stream = self._pa.open(
            format=pa.paFloat32,
            channels=self.channels,
            rate=self.rate,
            frames_per_buffer=CHUNK,
            input=True,
            input_device_index=self.device.index,
        )

        def pump() -> None:
            while not self._stop.is_set():
                try:
                    raw = self._stream.read(CHUNK, exception_on_overflow=False)
                except OSError:
                    break
                block = np.frombuffer(raw, dtype=np.float32).reshape(-1, self.channels)
                with self._lock:
                    self._frames.append(block.copy())

        self._stop.clear()
        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

        # 実際にフレームが流れ始めるまで待つ。来なければ黙って無音を返すより
        # はっきり失敗させる
        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if self.mark() > 0:
                return
            time.sleep(0.05)
        self.stop()
        raise AudioError(
            f"「{self.device.name}」からフレームが来ません。"
            "既定の再生デバイスが合っているか確認してください。"
        )

    def _start_keepalive(self, pa) -> None:
        """既定の再生デバイスに無音を流し続けるスレッドを起こす。"""
        if self.device.output_index is None:
            return
        info = self._pa.get_device_info_by_index(self.device.output_index)
        channels = max(1, int(info["maxOutputChannels"]))
        rate = int(info["defaultSampleRate"]) or self.rate
        try:
            self._silence = self._pa.open(
                format=pa.paFloat32,
                channels=channels,
                rate=rate,
                frames_per_buffer=CHUNK,
                output=True,
                output_device_index=self.device.output_index,
            )
        except OSError:
            self._silence = None
            return

        quiet = np.zeros(CHUNK * channels, dtype=np.float32).tobytes()

        def feed() -> None:
            while not self._stop.is_set():
                try:
                    self._silence.write(quiet)
                except OSError:
                    break

        self._silence_thread = threading.Thread(target=feed, daemon=True)
        self._silence_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._silence_thread:
            self._silence_thread.join(timeout=2.0)
            self._silence_thread = None
        if self._silence:
            self._silence.close()
            self._silence = None
        if self._stream:
            self._stream.close()
            self._stream = None
        if self._pa:
            self._pa.terminate()
            self._pa = None

    # -- 切り出し --------------------------------------------------------- #

    def mark(self) -> int:
        """いまの位置（フレーム数）を返す。"""
        with self._lock:
            return sum(len(f) for f in self._frames)

    def since(self, mark: int) -> np.ndarray:
        """``mark`` 以降を ``(n, channels)`` で返す。"""
        with self._lock:
            if not self._frames:
                return np.zeros((0, self.channels), dtype=np.float32)
            all_frames = np.concatenate(self._frames, axis=0)
        return all_frames[mark:]

    def capture(self, seconds: float) -> np.ndarray:
        """いまから ``seconds`` 秒ぶん録って返す。"""
        m = self.mark()
        time.sleep(seconds)
        return self.since(m)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def to_mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1) if audio.ndim > 1 else audio


def peak(audio: np.ndarray) -> float:
    return float(np.abs(audio).max()) if len(audio) else 0.0


def rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0


def onset_index(audio: np.ndarray, threshold_ratio: float = 0.15) -> int | None:
    """最初にピークの一定割合を超えた位置。無音なら None。"""
    mono = np.abs(to_mono(audio))
    if not len(mono):
        return None
    top = mono.max()
    if top <= 1e-5:
        return None
    above = np.nonzero(mono > top * threshold_ratio)[0]
    return int(above[0]) if len(above) else None


#: WASAPI の 7.1 は FL, FR, FC, LFE, BL, BR, SL, SR の順。
#: サラウンド対応デバイス（ヘッドセット等）だと 8ch で開くので、
#: 左右の判定は front だけでなく back / side も足す。
LEFT_CHANNELS = (0, 4, 6)
RIGHT_CHANNELS = (1, 5, 7)


def channel_balance(audio: np.ndarray) -> float:
    """ステレオ定位。-1 が完全に左、+1 が完全に右、0 が中央。"""
    if audio.ndim < 2 or audio.shape[1] < 2:
        return 0.0
    n = audio.shape[1]
    left = sum(rms(audio[:, c]) for c in LEFT_CHANNELS if c < n)
    right = sum(rms(audio[:, c]) for c in RIGHT_CHANNELS if c < n)
    total = left + right
    return float((right - left) / total) if total > 0 else 0.0


def dominant_frequency(audio: np.ndarray, rate: int, fmin: float = 40.0, fmax: float = 4200.0) -> float | None:
    """一番強い周波数成分。音程の実測に使う。"""
    mono = to_mono(audio)
    if len(mono) < 2048:
        return None
    window = np.hanning(len(mono))
    spectrum = np.abs(np.fft.rfft(mono * window))
    freqs = np.fft.rfftfreq(len(mono), 1 / rate)
    band = (freqs >= fmin) & (freqs <= fmax)
    if not band.any():
        return None
    idx = int(np.argmax(spectrum[band]))
    return float(freqs[band][idx])


def play_tone(device: Device, frequency: float = 440.0, seconds: float = 0.6, amplitude: float = 0.05) -> None:
    """テスト用の正弦波を鳴らす。録音経路が生きているか確かめるのに使う。"""
    pa = _pyaudio()
    if device.output_index is None:
        raise AudioError("再生デバイスが分からないのでテスト音を出せません。")
    with pa.PyAudio() as p:
        info = p.get_device_info_by_index(device.output_index)
        channels = max(1, int(info["maxOutputChannels"]))
        rate = int(info["defaultSampleRate"]) or TARGET_RATE
        t = np.arange(int(rate * seconds), dtype=np.float32) / rate
        # 頭と尻を滑らかにして、クリックノイズを混ぜない
        wave = np.sin(2 * np.pi * frequency * t) * amplitude
        fade = int(rate * 0.01)
        wave[:fade] *= np.linspace(0, 1, fade)
        wave[-fade:] *= np.linspace(1, 0, fade)
        block = np.repeat(wave[:, None], channels, axis=1).ravel()
        stream = p.open(
            format=pa.paFloat32, channels=channels, rate=rate, output=True,
            output_device_index=device.output_index,
        )
        stream.write(block.astype(np.float32).tobytes())
        stream.close()


def selftest(device: Device | None = None, frequency: float = 440.0) -> tuple[bool, str]:
    """テスト音を鳴らして、それがループバックで録れるか確かめる。"""
    device = device or default_loopback()
    with Recorder(device) as rec:
        mark = rec.mark()
        thread = threading.Thread(target=play_tone, args=(device, frequency), daemon=True)
        thread.start()
        time.sleep(1.2)
        thread.join(timeout=2.0)
        audio = rec.since(mark)
        rate = rec.rate

    level = peak(audio)
    if level < 1e-3:
        return False, f"テスト音が録れませんでした（ピーク {level:.6f}）。既定の再生デバイスを確認してください。"
    got = dominant_frequency(audio, rate, fmin=frequency / 2, fmax=frequency * 2)
    if got is None:
        return False, "録音はできましたが周波数を特定できませんでした。"
    error = abs(got - frequency) / frequency
    if error > 0.05:
        return False, f"{frequency:.0f} Hz を鳴らしたのに {got:.1f} Hz が録れました（ずれ {error:.1%}）。"
    return True, f"OK — {frequency:.0f} Hz を鳴らして {got:.1f} Hz を検出、ピーク {level:.4f}"


def save_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, rate)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="スピーカー出力のループバック録音")
    ap.add_argument("--devices", action="store_true", help="デバイスを一覧")
    ap.add_argument("--test", type=float, default=None, metavar="SEC", help="指定秒だけ録って情報を出す")
    ap.add_argument("--device", type=int, default=None, help="デバイス番号を明示")
    ap.add_argument("--save", type=Path, default=None, help="録音を WAV に保存")
    ap.add_argument("--selftest", action="store_true",
                    help="テスト音を鳴らして録音経路を確かめる（小さい音が一瞬鳴ります）")
    args = ap.parse_args(argv)

    try:
        if args.devices:
            for d in list_devices():
                print(d)
            print()
            print("既定のループバック:", default_loopback())
            return 0

        if args.selftest:
            ok, message = selftest()
            print(("✅ " if ok else "❌ ") + message)
            return 0 if ok else 1

        if args.test is None:
            ap.print_help()
            return 0

        device = None
        if args.device is not None:
            device = next((d for d in list_devices() if d.index == args.device), None)
            if device is None:
                print(f"デバイス {args.device} が見つかりません", file=sys.stderr)
                return 1

        with Recorder(device) as rec:
            print(f"デバイス: {rec.device.name}")
            print(f"{rec.rate} Hz / {rec.channels} ch / {args.test} 秒録音中…")
            audio = rec.capture(args.test)

        print(f"  フレーム数 : {len(audio)}")
        print(f"  ピーク     : {peak(audio):.6f}")
        print(f"  RMS        : {rms(audio):.6f}")
        print(f"  定位 (L-R) : {channel_balance(audio):+.3f}")
        onset = onset_index(audio)
        print(f"  最初の立ち上がり: {onset / rec.rate:.3f} 秒" if onset is not None else "  無音でした")
        if peak(audio) < 1e-4:
            print("\n  ⚠ ほぼ無音です。何か再生してから試すと、録れているか確認できます。")
        if args.save:
            save_wav(args.save, audio, rec.rate)
            print(f"  保存: {args.save}")

    except AudioError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
