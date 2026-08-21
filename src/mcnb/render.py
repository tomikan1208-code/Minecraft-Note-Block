"""配置を音にする — Minecraft を起動せずに「こう聞こえるはず」を合成する。

これが無いと編曲を変えても良くなったか分からない。v4（編曲最適化）の前提。

hyperchoron にも ``render_nbs`` があるが、こちらは別物:

* **抽出した本物の音源を使う。** hyperchoron は ``harp`` に ``note/harp.ogg`` を
  使っているが、26.2 が実際に鳴らすのは ``note/harp2.ogg``（``bass`` も同様）
* **配置から距離と定位を出す。** NBS の velocity/panning ではなく、
  実際に置いたブロックの座標で計算する
* **プレイヤーが動くことを勘定に入れる。** 直線コリドーでは 3 ブロック/tick =
  毎秒60ブロックで音源から遠ざかる。鳴らした音は鳴っている途中で減衰していく
* **同時発音の上限を掛ける。** バニラ 247 / RSLS 4095 を超えた音は鳴らない

    uv run mcnb render out/knights20/knights20.nbs --out out/knights20/mc.wav
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .layout import EAR_HEIGHT, MAX_HEARING, Layout

DEFAULT_RATE = 48000
#: ゲインと定位を計算し直す間隔（サンプル）。細かすぎても聴感は変わらない
BLOCK = 128
#: 音符ブロックの音源を置いてある場所
DEFAULT_ASSETS = Path("assets/mc")


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 音響モデル
# --------------------------------------------------------------------------- #


@dataclass
class AcousticModel:
    """Minecraft の聞こえ方のモデル。**v1 の実測で係数を差し替える前提。**"""

    #: 聞こえなくなる距離
    max_hearing: float = MAX_HEARING
    #: 距離とゲインの関係。実測で確かめるまでは線形と仮定している
    curve: str = "linear"
    #: 同時発音の上限（バニラ 247 / RSLS 4095）
    polyphony: int = 247
    #: 耳の高さ（足元からの相対）
    ear_height: float = EAR_HEIGHT
    #: 全体の音量。クリップしないように下げる
    master: float = 0.35
    #: 実測から較正した値かどうか
    calibrated: bool = False

    def gain(self, distance: np.ndarray) -> np.ndarray:
        if self.curve == "linear":
            return np.clip(1.0 - distance / self.max_hearing, 0.0, 1.0)
        if self.curve == "inverse":
            return np.clip(1.0 / np.maximum(distance, 1.0), 0.0, 1.0)
        raise RenderError(f"未知の減衰カーブ: {self.curve}")

    @classmethod
    def from_measurements(cls, path: Path) -> AcousticModel:
        """``mcnb measure`` の結果から較正する。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        section = data.get("sections", {}).get("distance")
        if not section:
            raise RenderError(f"{path} に距離減衰の測定がありません。")

        points = [p for p in section["points"] if p["peak"] > 1e-4]
        if len(points) < 4:
            raise RenderError("有効な測定点が足りません。")

        d = np.array([p["distance"] for p in points], dtype=float)
        a = np.array([p["peak"] for p in points], dtype=float)
        a = a / a.max()

        # 聞こえなくなる距離は「無音だった点のうち一番近いもの」
        silent = [p["distance"] for p in section["points"] if p["peak"] <= 1e-4]
        max_hearing = float(min(silent)) if silent else MAX_HEARING

        model = cls(max_hearing=max_hearing, calibrated=True)
        linear = np.clip(1 - d / max_hearing, 0, None)
        inverse = 1 / np.maximum(d, 1.0)
        err_lin = np.sqrt(np.mean((a - linear / max(linear.max(), 1e-9)) ** 2))
        err_inv = np.sqrt(np.mean((a - inverse / inverse.max()) ** 2))
        model.curve = "linear" if err_lin <= err_inv else "inverse"
        return model


# --------------------------------------------------------------------------- #
# 音源
# --------------------------------------------------------------------------- #


class SampleBank:
    """抽出した音源を読み、音程ごとにリサンプルして持っておく。

    音符ブロックの音程は**再生速度で作られる**（音源は1つだけ）。
    note ステート n の再生倍率は ``2^((n-12)/12)``。
    """

    def __init__(self, assets_dir: Path | None = None, rate: int = DEFAULT_RATE):
        self.dir = Path(assets_dir or DEFAULT_ASSETS)
        self.rate = rate
        self._base: dict[str, np.ndarray] = {}
        self._pitched: dict[tuple[str, int], np.ndarray] = {}

        manifest = self.dir / "manifest.json"
        if not manifest.is_file():
            raise RenderError(
                f"{manifest} がありません。先に `mcnb setup`（音源の抽出）を実行してください。"
            )
        self.manifest = json.loads(manifest.read_text(encoding="utf-8"))

    def _load_base(self, instrument: str) -> np.ndarray:
        if instrument in self._base:
            return self._base[instrument]

        import librosa

        entry = next(
            (s for s in self.manifest["samples"] if s["instrument"] == instrument), None
        )
        if entry is None:
            raise RenderError(f"音源が見つかりません: {instrument}")
        # ランダムに選ばれる楽器（Mob ヘッド）は先頭で代表させる
        path = self.dir / entry["files"][0]["file"]
        audio, sr = librosa.load(str(path), sr=self.rate, mono=True)
        audio = audio.astype(np.float32) * float(entry["files"][0].get("volume", 1.0))
        pitch = float(entry["files"][0].get("pitch", 1.0))
        if pitch != 1.0:
            audio = self._shift(audio, pitch)
        self._base[instrument] = audio
        return audio

    def _shift(self, audio: np.ndarray, ratio: float) -> np.ndarray:
        """再生倍率 ``ratio`` で鳴らしたときの波形（速く＝高く、短く）。"""
        if abs(ratio - 1.0) < 1e-6:
            return audio
        import librosa

        target = max(1, int(round(self.rate / ratio)))
        return librosa.resample(audio, orig_sr=self.rate, target_sr=target).astype(np.float32)

    def get(self, instrument: str, key: int) -> np.ndarray:
        """note ステート ``key`` で鳴らしたときの波形。"""
        cached = self._pitched.get((instrument, key))
        if cached is not None:
            return cached
        shifted = self._shift(self._load_base(instrument), 2 ** ((key - 12) / 12))
        self._pitched[(instrument, key)] = shifted
        return shifted


# --------------------------------------------------------------------------- #
# レンダリング
# --------------------------------------------------------------------------- #


@dataclass
class RenderResult:
    audio: np.ndarray  # (n, 2)
    rate: int
    played: int
    dropped_polyphony: int
    peak_polyphony: int

    @property
    def duration(self) -> float:
        return len(self.audio) / self.rate

    def summary(self) -> str:
        lines = [
            f"長さ        : {self.duration:.1f} 秒",
            f"鳴った音    : {self.played}",
            f"最大同時発音: {self.peak_polyphony}",
            f"ピーク      : {float(np.abs(self.audio).max()):.3f}",
        ]
        if self.dropped_polyphony:
            lines.append(f"同時発音上限で落ちた音: {self.dropped_polyphony}")
        return "\n".join(lines)


def render_layout(
    layout: Layout,
    bank: SampleBank | None = None,
    model: AcousticModel | None = None,
    rate: int = DEFAULT_RATE,
    tail: float = 2.0,
) -> RenderResult:
    """配置をそのまま音にする。プレイヤーが動くことも勘定に入れる。"""
    bank = bank or SampleBank(rate=rate)
    model = model or AcousticModel()

    x0, y0, z0 = layout.origin
    ear_y = y0 + model.ear_height
    #: プレイヤーが遠ざかる速さ（ブロック/秒）
    speed = layout.spacing * 20.0

    total = int((layout.song.length_ticks / 20.0 + tail) * rate) + rate
    buffer = np.zeros((total, 2), dtype=np.float32)

    events = sorted(layout.placements, key=lambda p: p.tick)
    active_until: list[int] = []
    played = dropped = peak_poly = 0

    for p in events:
        start = int(round(p.tick / 20.0 * rate))

        # 上限に達していたら、この音は鳴らない（Minecraft は新しい音を捨てる）
        active_until = [e for e in active_until if e > start]
        if len(active_until) >= model.polyphony:
            dropped += 1
            continue

        try:
            sample = bank.get(p.instrument, p.key)
        except RenderError:
            dropped += 1
            continue

        n = len(sample)
        if start + n > total:
            n = total - start
            if n <= 0:
                continue
            sample = sample[:n]

        active_until.append(start + n)
        peak_poly = max(peak_poly, len(active_until))
        played += 1

        # 音が鳴っている間、プレイヤーは +X へ離れていく。
        # 発音した瞬間はちょうど同じ平面にいるので、X 差は経過時間だけで決まる。
        dy = p.y - ear_y
        dz = p.z - z0
        flat = dy * dy + dz * dz

        idx = np.arange(0, n, BLOCK)
        elapsed = idx / rate
        dx = speed * elapsed
        distance = np.sqrt(dx * dx + flat)
        gains = model.gain(distance)
        # +X を向いているので +Z が右。真横に来るほど定位が振れる
        pans = np.where(distance > 1e-6, dz / np.maximum(distance, 1e-6), 0.0)

        angle = (np.clip(pans, -1, 1) + 1) * (np.pi / 4)
        left = np.cos(angle) * gains
        right = np.sin(angle) * gains

        # ブロックごとの係数をサンプル数に伸ばす
        counts = np.diff(np.append(idx, n))
        left = np.repeat(left, counts)
        right = np.repeat(right, counts)

        buffer[start:start + n, 0] += sample * left
        buffer[start:start + n, 1] += sample * right

    buffer *= model.master
    # クリップさせない。全体を下げるだけなので相対的な音量差は保たれる
    peak = float(np.abs(buffer).max())
    if peak > 1.0:
        buffer /= peak

    # 末尾の無音を落とす
    loud = np.nonzero(np.abs(buffer).max(axis=1) > 1e-5)[0]
    if len(loud):
        buffer = buffer[: min(total, loud[-1] + rate // 2)]

    return RenderResult(
        audio=buffer,
        rate=rate,
        played=played,
        dropped_polyphony=dropped,
        peak_polyphony=peak_poly,
    )


def summary_indent(result: RenderResult) -> str:
    return "\n".join("  " + line for line in result.summary().splitlines())


def save(result: RenderResult, path: Path) -> Path:
    import soundfile as sf

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), result.audio, result.rate)
    return path


# --------------------------------------------------------------------------- #
# 目で確かめる
# --------------------------------------------------------------------------- #


def plot_compare(
    rendered: Path, original: Path | None, out_path: Path, seconds: float = 20.0
) -> Path:
    """スペクトログラムを並べる。数値だけだと何が起きているか見えない。"""
    import librosa
    import librosa.display
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ("Meiryo", "Yu Gothic", "MS Gothic", "Noto Sans CJK JP"):
        if candidate in available:
            plt.rcParams["font.family"] = candidate
            break
    plt.rcParams["axes.unicode_minus"] = False

    tracks = [("Minecraft 版", rendered)]
    if original and Path(original).is_file():
        tracks.insert(0, ("原曲", original))

    fig, axes = plt.subplots(len(tracks), 1, figsize=(11, 3.2 * len(tracks)), sharex=True)
    if len(tracks) == 1:
        axes = [axes]

    for ax, (label, path) in zip(axes, tracks, strict=False):
        y, sr = librosa.load(str(path), sr=22050, mono=True, duration=seconds)
        spec = librosa.amplitude_to_db(np.abs(librosa.stft(y, n_fft=2048, hop_length=256)), ref=np.max)
        librosa.display.specshow(spec, sr=sr, hop_length=256, x_axis="time", y_axis="log", ax=ax)
        ax.set_title(label)
        ax.set_ylabel("周波数 (Hz)")
    axes[-1].set_xlabel("時間 (秒)")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=100)
    plt.close(fig)
    return out_path


# --------------------------------------------------------------------------- #
# 原曲との距離
# --------------------------------------------------------------------------- #

#: 帯域ごとの過不足を見るための区切り（Hz）。
#: 音符ブロックの最低音 F♯1 = 46.25 Hz なので、そこを下回る帯は原理的に出せない
BANDS = [(0, 128, "低域"), (128, 512, "中低域"), (512, 2048, "中高域"), (2048, 11025, "高域")]


def compare(original: Path, rendered: Path, seconds: float = 30.0) -> dict:
    """原曲と Minecraft 版の距離を測る。

    **単一の指標に最適化すると音楽的に壊れる**ので、性質ごとに分けて出す。
    どれも 1.0 が「一致」。
    """
    import librosa

    sr = 22050
    a, _ = librosa.load(str(original), sr=sr, mono=True, duration=seconds)
    b, _ = librosa.load(str(rendered), sr=sr, mono=True, duration=seconds)
    n = min(len(a), len(b))
    if n < sr:
        raise RenderError("比較するには短すぎます。")
    a, b = a[:n], b[:n]

    hop = 512

    def _corr(x: np.ndarray, y: np.ndarray) -> float:
        m = min(len(x), len(y))
        x, y = x[:m], y[:m]
        if x.std() < 1e-9 or y.std() < 1e-9:
            return 0.0
        return float(np.corrcoef(x, y)[0, 1])

    # 和声: ピッチクラスの分布がどれだけ似ているか
    ca = librosa.feature.chroma_cqt(y=a, sr=sr, hop_length=hop)
    cb = librosa.feature.chroma_cqt(y=b, sr=sr, hop_length=hop)
    m = min(ca.shape[1], cb.shape[1])
    ca, cb = ca[:, :m], cb[:, :m]
    denom = np.linalg.norm(ca, axis=0) * np.linalg.norm(cb, axis=0)
    chroma = float(np.mean(np.where(denom > 1e-9, (ca * cb).sum(axis=0) / np.maximum(denom, 1e-9), 0)))

    # リズム: いつ音が立ち上がるか
    onset = _corr(
        librosa.onset.onset_strength(y=a, sr=sr, hop_length=hop),
        librosa.onset.onset_strength(y=b, sr=sr, hop_length=hop),
    )

    # ダイナミクス: 音量の動き
    loudness = _corr(
        librosa.feature.rms(y=a, hop_length=hop)[0],
        librosa.feature.rms(y=b, hop_length=hop)[0],
    )

    # 音色: 明るさの動き
    centroid = _corr(
        librosa.feature.spectral_centroid(y=a, sr=sr, hop_length=hop)[0],
        librosa.feature.spectral_centroid(y=b, sr=sr, hop_length=hop)[0],
    )

    # 帯域ごとのエネルギー比。どこが足りない/多すぎるかを直接見る
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    sa = np.abs(librosa.stft(a, n_fft=2048, hop_length=hop))
    sb = np.abs(librosa.stft(b, n_fft=2048, hop_length=hop))
    bands = {}
    for lo, hi, label in BANDS:
        sel = (freqs >= lo) & (freqs < hi)
        ea, eb = float(sa[sel].mean()), float(sb[sel].mean())
        bands[label] = {
            "原曲": round(ea, 5),
            "MC版": round(eb, 5),
            "比": round(eb / ea, 3) if ea > 1e-9 else None,
        }

    return {
        "chroma": round(chroma, 3),
        "onset": round(onset, 3),
        "loudness": round(loudness, 3),
        "centroid": round(centroid, 3),
        "bands": bands,
    }


def format_compare(result: dict) -> str:
    lines = [
        f"  和声 (chroma)     {result['chroma']:+.3f}",
        f"  リズム (onset)    {result['onset']:+.3f}",
        f"  強弱 (loudness)   {result['loudness']:+.3f}",
        f"  明るさ (centroid) {result['centroid']:+.3f}",
        "",
        "  帯域エネルギー比 (MC版 / 原曲):",
    ]
    for label, v in result["bands"].items():
        ratio = v["比"]
        mark = ""
        if ratio is not None:
            if ratio < 0.5:
                mark = "  ← 足りない"
            elif ratio > 2.0:
                mark = "  ← 出すぎ"
        lines.append(f"    {label:6s} {ratio if ratio is not None else '—'}{mark}")
    return "\n".join(lines)
