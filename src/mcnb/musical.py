"""原音から音楽的な文脈を取り出す — 拍・調・コード進行・主旋律。

これまでの編曲は「音符を機械的に間引く」だけで、**何の和音なのか / どれが主旋律か**を
知らなかった。だから「そのまま音を再現しているだけ」になっていた。

ここが入ると、音符ひとつひとつに意味がつく::

    この音は今のコードの構成音か   → 残す
    この音はコード外で小さい       → 採譜の誤り（倍音・ノイズ）の可能性が高い
    この音は主旋律のライン上にある → 前に出す

**採譜の結果からではなく原音から取る。** 採譜がスペクトルのピーク拾いで
壊れているので、そこからコードを推定しても壊れた答えしか出ない。

    uv run python -m mcnb.musical cache/audio/song.wav
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

#: 解析のサンプルレート。コードと主旋律にはこれで十分
ANALYSIS_RATE = 22050
#: chroma / salience のホップ長。22050Hz で約 23ms
HOP = 512

PITCH_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

#: 解析結果の版。項目を足したり意味を変えたら上げる。
#: 古い結果を既定値で埋めて読むと「分からない」が「確かだ」に化けるので、
#: 版が違うものは読まずに解析し直す。
ANALYSIS_VERSION = 1

# --------------------------------------------------------------------------- #
# 調とコードの定義
# --------------------------------------------------------------------------- #

#: Krumhansl-Schmuckler の調プロファイル。各音がその調でどれだけ「らしい」か
MAJOR_PROFILE = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88], dtype=np.float32
)
MINOR_PROFILE = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17], dtype=np.float32
)

MAJOR_SCALE = (0, 2, 4, 5, 7, 9, 11)
MINOR_SCALE = (0, 2, 3, 5, 7, 8, 10)
#: 和声的短音階の導音。短調の V が長三和音になるのはこれのおかげだが、
#: 音階そのものに足してしまうと **導音を含むだけの無関係なコード** まで
#: 調内扱いになる。V のときだけ許す。
LEADING_TONE = 11

#: コードの種類と構成音（根音からの半音）、および「選ばれにくさ」。
#: sus4 と dim は隣り合うコードとも重なるので黙っていると勝ちすぎる。
#: 7th も構成音が多いぶん密な曲では有利になるので、少し不利にしておく。
CHORD_TEMPLATES: dict[str, tuple[tuple[int, ...], float]] = {
    "":     ((0, 4, 7),      0.00),  # メジャー
    "m":    ((0, 3, 7),      0.00),  # マイナー
    "7":    ((0, 4, 7, 10),  0.04),
    "m7":   ((0, 3, 7, 10),  0.04),
    "maj7": ((0, 4, 7, 11),  0.05),
    "dim":  ((0, 3, 6),      0.08),
    "sus4": ((0, 5, 7),      0.08),
}

#: 同じコードに留まりやすくするための重み。高いほどコードが切り替わりにくい。
#: 合成音での実測では 0.18〜0.45 のあいだ一致率が 85% で変わらない。
#: 平坦なのでどこを取ってもよく、その真ん中あたりを選んである。
#: 0.14 以下に下げると 73%、0.10 で 57% まで落ちる — 拍ごとに揺れるようになる。
CHORD_SELF_BONUS = 0.22
#: 調に収まるコードへのおまけ。構成音が全部調内なら満額
DIATONIC_BONUS = 0.10
#: 調を決めるとき、コード進行との整合をクロマの偏りに対してどれだけ重く見るか。
#: クロマだけでは平行調・同主調が決まらないので、ここが要る。
KEY_CHORD_WEIGHT = 0.6
#: 調を見る窓の長さ（秒）。曲の途中で転調しても追えるようにするため。
#: 短くすると転調に速く追従するが、部分的なコードの揺れも転調と見なしてしまう。
KEY_WINDOW = 20.0
#: 窓をずらす幅（秒）
KEY_HOP = 5.0
#: 同じ調に留まりやすくする重み。転調はそう頻繁には起きない
KEY_SELF_BONUS = 0.5
#: 「コードなし」と判断する境目。**曲ごとの手応えの中央値に対する比**で決める。
#: 絶対値で決めると曲をまたげない。0.55 固定で測ると、真っ黒ピアノでは 6% が N
#: なのに歌ものでは 31% が N になった — 曲全体の相関の高さが編成や音の混み具合で
#: 変わるだけで、「コードが無い」かどうかとは関係がない。
#: 中央値比にすると両方 2〜5% に揃う。
NO_CHORD_RATIO = 0.63

#: 主旋律を探す音域
MELODY_LO_MIDI = 48   # C3
MELODY_HI_MIDI = 96   # C7
#: 倍音を足して基音の手応えを出すときの重み（2倍音・3倍音…）
HARMONIC_WEIGHTS = (1.0, 0.5, 0.33, 0.25)
#: 半音動くごとの罰。大きいほど旋律が滑らかになる
MELODY_JUMP_PENALTY = 0.25
#: 声あり／なしを行き来する罰
MELODY_VOICING_PENALTY = 0.6
#: これ以下の強さしかないフレームは「鳴っていない」とみなす（全体最大からの dB）
MELODY_FLOOR_DB = -36.0
#: 高い声部を主旋律とみなす度合い。0 で音域による贔屓なし。
#: 多声の中から旋律を選ぶときは、これが無いと一番大きいベースを拾ってしまう。
#: ただし**単声（分離したボーカル）には有害**。避けるべき低音が無いのに
#: 高いほうへ引っぱるので、そのままオクターブずれになる。
#: 分離したボーカルで実測: 下駄 0.35 で pyin と一致 37%(1oct ずれ 13%)、
#: 下駄 0.00 で一致 72%(1oct ずれ 1%)。
MELODY_HIGH_BIAS = 0.35
#: 単声の音源から取るときの下駄
MELODY_HIGH_BIAS_MONO = 0.0
#: 声トラックがこれ未満の音量しか無ければ「ボーカル無し」とみなす（原音との比）
VOCAL_PRESENCE_RATIO = 0.05


@dataclass(frozen=True)
class Key:
    """曲の調。"""

    tonic: int          # 0-11
    minor: bool
    confidence: float

    @property
    def name(self) -> str:
        return PITCH_NAMES[self.tonic] + ("m" if self.minor else "")

    @property
    def scale(self) -> tuple[int, ...]:
        steps = MINOR_SCALE if self.minor else MAJOR_SCALE
        return tuple((self.tonic + s) % 12 for s in steps)


@dataclass(frozen=True)
class KeySpan:
    """ある区間の調。転調する曲では複数になる。"""

    start: float
    end: float
    key: Key


@dataclass(frozen=True)
class Chord:
    """ある区間で鳴っているコード。"""

    start: float          # 秒
    end: float
    root: int             # 0-11、-1 は「コードなし」
    quality: str
    confidence: float

    @property
    def name(self) -> str:
        return "N" if self.root < 0 else PITCH_NAMES[self.root] + self.quality

    @property
    def pitch_classes(self) -> tuple[int, ...]:
        """構成音のピッチクラス。"""
        if self.root < 0:
            return ()
        return tuple((self.root + i) % 12 for i in CHORD_TEMPLATES[self.quality][0])

    def contains(self, midi: int) -> bool:
        return (midi % 12) in self.pitch_classes

    def degree(self, midi: int) -> int | None:
        """構成音の何番目か（0=根音, 1=第3音, 2=第5音…）。構成音でなければ None。"""
        pcs = self.pitch_classes
        pc = midi % 12
        return pcs.index(pc) if pc in pcs else None


@dataclass
class MusicalContext:
    """原音から取り出した音楽的な文脈。"""

    tempo: float
    beats: list[float] = field(default_factory=list)        # 秒
    downbeats: list[float] = field(default_factory=list)
    key: Key | None = None
    #: 区間ごとの調。転調しなければ 1 個
    keys: list[KeySpan] = field(default_factory=list)
    chords: list[Chord] = field(default_factory=list)
    #: 主旋律。(秒, MIDIノート番号)。鳴っていない区間は入らない
    melody: list[tuple[float, float]] = field(default_factory=list)
    duration: float = 0.0
    #: 1小節あたりの拍数
    beats_per_bar: int = 4
    #: 拍子が曲を通して一定か（窓ごとの一致率）。低ければ変拍子か、決められていない
    meter_stability: float = 1.0
    #: 主旋律をどこから取ったか。"vocals"（分離した声）か "mix"（混ざったまま）
    melody_source: str = "mix"
    #: 伴奏だけの音（カラオケ）。分離したときだけ入る
    instrumental: str | None = None

    # -- 問い合わせ ------------------------------------------------------- #

    def chord_at(self, seconds: float) -> Chord | None:
        """その時刻のコード。二分探索なので音符ごとに呼んでよい。"""
        lo, hi = 0, len(self.chords)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.chords[mid].end <= seconds:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(self.chords) and self.chords[lo].start <= seconds:
            return self.chords[lo]
        return None

    def melody_at(self, seconds: float, window: float = 0.08) -> float | None:
        """その時刻あたりの主旋律の音高（MIDI、小数）。"""
        if not self.melody:
            return None
        times = self._melody_times
        i = int(np.searchsorted(times, seconds))
        best: tuple[float, float] | None = None
        for j in (i - 1, i):
            if 0 <= j < len(self.melody):
                gap = abs(self.melody[j][0] - seconds)
                if gap <= window and (best is None or gap < best[0]):
                    best = (gap, self.melody[j][1])
        return best[1] if best else None

    @property
    def _melody_times(self) -> np.ndarray:
        cached = getattr(self, "_mt_cache", None)
        if cached is None or len(cached) != len(self.melody):
            cached = np.array([t for t, _ in self.melody])
            self._mt_cache = cached
        return cached

    def key_at(self, seconds: float) -> Key | None:
        """その時刻の調。転調を追っているので場所で変わる。"""
        for span in self.keys:
            if span.start <= seconds < span.end:
                return span.key
        return self.key

    def beat_index(self, seconds: float) -> int:
        """その時刻が何拍目か。拍より前なら -1。"""
        return int(np.searchsorted(self.beats, seconds, side="right")) - 1

    def metrical_position(self, seconds: float) -> tuple[int, float] | None:
        """その時刻が「何小節目の、小節頭から何拍のところ」かを返す。

        小節内の位置は小数。1.5 なら 2 拍目の裏。編曲でどの音を残すかを
        決めるとき、**同じ音でも小節のどこにあるかで重みが違う**ので要る。
        """
        i = self.beat_index(seconds)
        if i < 0 or not self.beats:
            return None
        if i + 1 < len(self.beats):
            span = self.beats[i + 1] - self.beats[i]
        else:
            span = self.beats[i] - self.beats[i - 1] if len(self.beats) > 1 else 1.0
        within = (seconds - self.beats[i]) / span if span > 0 else 0.0

        # 小節頭からの通し拍数。downbeats は beats の部分列
        offset = int(np.searchsorted(self.downbeats, self.beats[i], side="right")) - 1
        if offset < 0:
            first = self.beat_index(self.downbeats[0]) if self.downbeats else 0
            return -1, float((i - first) % self.beats_per_bar) + within
        head = self.beat_index(self.downbeats[offset])
        return offset, float(i - head) + within

    @property
    def meter_is_reliable(self) -> bool:
        """小節の位置を当てにしてよいか。

        変拍子や、拍子が決まらない曲では小節頭が定まらない。そのときに
        「小節頭だから残す」と判断すると、**でたらめな位置の音を優遇する**ことになる。
        当てにできないなら拍だけで済ませるほうがよい。
        """
        return self.meter_stability >= METER_STABLE

    def is_downbeat(self, seconds: float, tolerance: float = 0.25) -> bool:
        """小節頭のあたりか。``tolerance`` は拍を 1 とした割合。

        拍子が当てにならない曲では、常に False を返す。
        """
        if not self.meter_is_reliable:
            return False
        position = self.metrical_position(seconds)
        if position is None:
            return False
        within = position[1] % self.beats_per_bar
        return min(within, self.beats_per_bar - within) <= tolerance

    def summary(self) -> str:
        uniq: list[str] = []
        for c in self.chords:
            if not uniq or uniq[-1] != c.name:
                uniq.append(c.name)
        melody_seconds = len(self.melody) * HOP / ANALYSIS_RATE
        share = melody_seconds / self.duration * 100 if self.duration else 0.0
        return "\n".join(
            [
                f"長さ    : {self.duration:.1f} 秒",
                f"テンポ  : {self.tempo:.1f} BPM  拍 {len(self.beats)} 個",
                "拍子    : "
                + (
                    f"{self.beats_per_bar}拍子  小節 {len(self.downbeats)} 個"
                    f"  (一致率 {self.meter_stability:.0%})"
                    if self.meter_is_reliable
                    else f"一定でない（最頻は {self.beats_per_bar}拍子だが一致率 "
                    f"{self.meter_stability:.0%}）— 変拍子とみなして小節は使わない"
                ),
                f"調      : {self.key.name if self.key else '不明'}"
                + (f"  (確信度 {self.key.confidence:.2f})" if self.key else "")
                + (
                    "  転調 " + " → ".join(s.key.name for s in self.keys)
                    if len(self.keys) > 1
                    else ""
                ),
                f"コード  : {len(self.chords)} 区間 / 異なり {len(set(c.name for c in self.chords))} 種",
                f"  進行  : {' → '.join(uniq[:20])}" + (" …" if len(uniq) > 20 else ""),
                f"主旋律  : {melody_seconds:.1f} 秒ぶん ({share:.0f}%)"
                + ("  ← 分離した声から" if self.melody_source == "vocals" else "  ← 混ざったまま")
                + (
                    f"  音域 {_midi_name(min(p for _, p in self.melody))}"
                    f"〜{_midi_name(max(p for _, p in self.melody))}"
                    if self.melody
                    else ""
                ),
            ]
        )

    def to_dict(self) -> dict:
        return {
            "version": ANALYSIS_VERSION,
            "tempo": self.tempo,
            "duration": self.duration,
            "beats": self.beats,
            "downbeats": self.downbeats,
            "beats_per_bar": self.beats_per_bar,
            "meter_stability": self.meter_stability,
            "key": ({**asdict(self.key), "name": self.key.name} if self.key else None),
            "keys": [
                {"start": s.start, "end": s.end, **asdict(s.key), "name": s.key.name}
                for s in self.keys
            ],
            "chords": [{**asdict(c), "name": c.name} for c in self.chords],
            "melody": [[t, p] for t, p in self.melody],
            "melody_source": self.melody_source,
            "instrumental": self.instrumental,
        }


    @classmethod
    def from_dict(cls, data: dict) -> MusicalContext:
        """``to_dict()`` で書いたものを読み戻す。

        版が違えば ``ValueError``。古い結果には後から足した項目が無く、
        既定値で埋めると **「分からない」が「確かだ」に化ける**。
        たとえば meter_stability が無ければ 1.0（拍子は当てにしてよい）になり、
        変拍子の曲で小節頭を主張してしまう。
        """
        if data.get("version") != ANALYSIS_VERSION:
            raise ValueError(
                f"解析結果の版が違います（{data.get('version')} ≠ {ANALYSIS_VERSION}）"
            )
        key = Key(**{k: data["key"][k] for k in ("tonic", "minor", "confidence")}) if data.get("key") else None
        return cls(
            tempo=data["tempo"],
            beats=list(data.get("beats", [])),
            downbeats=list(data.get("downbeats", [])),
            key=key,
            keys=[
                KeySpan(
                    start=s["start"], end=s["end"],
                    key=Key(tonic=s["tonic"], minor=s["minor"], confidence=s["confidence"]),
                )
                for s in data.get("keys", [])
            ],
            chords=[
                Chord(**{k: c[k] for k in ("start", "end", "root", "quality", "confidence")})
                for c in data.get("chords", [])
            ],
            melody=[(float(t), float(p)) for t, p in data.get("melody", [])],
            duration=data.get("duration", 0.0),
            beats_per_bar=data.get("beats_per_bar", 4),
            meter_stability=data.get("meter_stability", 1.0),
            melody_source=data.get("melody_source", "mix"),
            instrumental=data.get("instrumental"),
        )


def _midi_name(midi: float) -> str:
    n = int(round(midi))
    return f"{PITCH_NAMES[n % 12]}{n // 12 - 1}"


# --------------------------------------------------------------------------- #
# 拍
# --------------------------------------------------------------------------- #


def track_beats(y: np.ndarray, sr: int) -> tuple[float, np.ndarray]:
    """拍を取る。テンポの倍・半分の取り違えを自前で直す。

    librosa の推定はしばしば本当のテンポの半分（または倍）に落ちる。
    どちらが正しいかは、**その拍の格子がオンセットをどれだけ説明できるか**で決める。
    格子が粗すぎればオンセットを取りこぼし、細かすぎれば空振りの拍が増えるので、
    F 値（適合率と再現率の調和平均）が素直に最大になる。
    """
    import librosa

    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP, aggregate=np.median)
    onsets = librosa.onset.onset_detect(onset_envelope=onset, sr=sr, hop_length=HOP, units="frames")
    tolerance = 3  # フレーム ≒ 70ms

    def grid(hint: float) -> tuple[float, np.ndarray, float]:
        tempo, frames = librosa.beat.beat_track(
            onset_envelope=onset, sr=sr, hop_length=HOP, start_bpm=hint, units="frames"
        )
        bpm = float(np.atleast_1d(tempo)[0])
        if len(frames) == 0 or len(onsets) == 0:
            return bpm, frames, 0.0
        hits = sum(1 for o in onsets if np.min(np.abs(frames - o)) <= tolerance)
        precision = hits / len(frames)
        recall = hits / len(onsets)
        f = 2 * precision * recall / max(precision + recall, 1e-9)
        return bpm, frames, f

    best = grid(120.0)
    seen = {round(best[0], 1)}
    # 半分・倍を試す。人が拍と感じる範囲（おおむね 50〜210 BPM）に収まるものだけ
    for factor in (0.5, 2.0, 4.0):
        hint = best[0] * factor
        if not 50.0 <= hint <= 210.0:
            continue
        cand = grid(hint)
        if round(cand[0], 1) in seen:
            continue
        seen.add(round(cand[0], 1))
        if cand[2] > best[2]:
            best = cand

    return best[0], best[1]


#: 1小節あたりの拍数の候補。
#: 8 まで見るのが要る。拍の推定が8分音符に乗ると 4/4 が 1小節 8拍になるので、
#: 7 で打ち切ると **8拍子の曲が 7拍子として溢れる**（ナイツで実際に起きた）。
#: 6 と 8 は「拍が8分に乗った 3拍子 / 4拍子」の意味だと思ってよい。
METER_CANDIDATES = (2, 3, 4, 5, 6, 7, 8)
#: コードの変わり目が小節頭に乗っているかを、拍の強さに対してどれだけ重く見るか。
#: 拍の強さだけだと「2拍子」が常に有利になる（4拍子の曲は2拍子でも説明できる）。
#: コードの変わり目は小節の長さで周期を持つので、そこを分ける決め手になる。
METER_CHORD_WEIGHT = 1.5
#: 拍子が曲を通して一定かを確かめる窓の長さ（拍）と、ずらす幅
METER_WINDOW_BEATS = 48
METER_WINDOW_HOP = 24
#: 窓の下限（拍）。estimate_meter は候補の 2 倍の拍数を要るので、それより下げない
METER_WINDOW_MIN = 16
#: 窓ごとの拍子がこれだけ揃わなければ、「曲を通してひとつの拍子」という
#: 前提そのものが成り立っていないとみなす。
#: 実測: 拍子が一定の合成音 100% / ナイツ 62% / Crazy 65% / 疾駆流金(変拍子) 33%
METER_STABLE = 0.5


def estimate_meter(
    beat_strength: np.ndarray, beats: np.ndarray, chords: list[Chord] | None
) -> tuple[int, int]:
    """1小節あたりの拍数と、小節頭がどの拍から始まるかを返す。

    手がかりは 2 つ。

    * **小節頭は強い** — 拍ごとのオンセットの強さが、小節の長さで周期を持つ
    * **コードは小節頭で変わる** — 変わり目の位置も同じ周期を持つ

    強さだけだと 2 拍子が常に勝つ（4 拍子の曲は 2 拍子でも説明できてしまう）。
    そこで**まぐれで当たる分を引く**。周期 P なら、でたらめに置いても
    1/P は小節頭に当たるので、そこを超えた分だけを点数にする。
    """
    if len(beat_strength) < max(METER_CANDIDATES) * 2:
        return 4, 0

    changes: list[int] = []
    if chords:
        for chord in chords[1:]:
            if chord.root < 0:
                continue
            changes.append(int(np.argmin(np.abs(beats - chord.start))))

    mean_strength = float(np.mean(beat_strength)) or 1.0
    best = (4, 0, -1e9)
    for per_bar in METER_CANDIDATES:
        for phase in range(per_bar):
            downbeats = beat_strength[phase::per_bar]
            if len(downbeats) < 2:
                continue
            # 小節頭がどれだけ強いか（1.0 が「他の拍と同じ」）
            strength = float(np.mean(downbeats)) / mean_strength - 1.0
            score = strength
            if changes:
                on_downbeat = sum(1 for i in changes if i % per_bar == phase) / len(changes)
                score += METER_CHORD_WEIGHT * (on_downbeat - 1.0 / per_bar)
            if score > best[2]:
                best = (per_bar, phase, score)
    return best[0], best[1]


def meter_stability(
    beat_strength: np.ndarray, beats: np.ndarray, chords: list[Chord] | None
) -> float:
    """拍子が曲を通して一定か。窓ごとに出して、最頻がどれだけ占めるかを返す。

    「拍子が分からない」と「拍子が途中で変わる」は、一箇所だけ見ても区別できない。
    どちらも同じ答えの出方をする。**窓ごとに出して揃うかどうか**を見れば、
    曲を通してひとつの拍子という前提が成り立っているかが分かる。
    """
    # 短い曲では窓を詰める。窓ひとつ分に満たないからといって
    # 「一定でない」と答えてはいけない — それは**調べられなかった**であって、
    # ばらついていることの証拠ではない。
    window = min(METER_WINDOW_BEATS, max(METER_WINDOW_MIN, len(beat_strength) // 3))
    hop = max(1, window // 2)

    votes: list[int] = []
    for start in range(0, max(len(beat_strength) - window, 0) + 1, hop):
        segment = beat_strength[start : start + window]
        if len(segment) < window:
            break
        span = beats[start : start + window]
        local = [c for c in (chords or []) if c.end > span[0] and c.start < span[-1]]
        votes.append(estimate_meter(segment, span, local)[0])
    if len(votes) < 2:
        # 比べる相手がいない。ばらつきを示せないので、一定として扱う
        return 1.0
    return max(votes.count(v) for v in set(votes)) / len(votes)


def find_downbeats(
    y: np.ndarray, sr: int, beat_frames: np.ndarray, chords: list[Chord] | None = None
) -> tuple[list[int], int, float]:
    """小節頭がどの拍かの一覧・1小節あたりの拍数・拍子の確信度を返す。"""
    import librosa

    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP, aggregate=np.median)
    strength = onset[np.clip(beat_frames, 0, len(onset) - 1)]
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    per_bar, phase = estimate_meter(strength, beats, chords)
    stability = meter_stability(strength, beats, chords)
    return list(range(phase, len(beat_frames), per_bar)), per_bar, stability


# --------------------------------------------------------------------------- #
# 調
# --------------------------------------------------------------------------- #


def separate_harmonic(y: np.ndarray) -> np.ndarray:
    """打楽器成分を落とす。太鼓やシンバルは音程を持たないので邪魔になる。"""
    import librosa

    return librosa.effects.harmonic(y, margin=3.0)


def chord_chroma(harmonic: np.ndarray, sr: int) -> np.ndarray:
    """コード推定・調推定に使うクロマ。打楽器を落とした音を渡すこと。

    音域は絞っていない。「和音は低〜中音域で決まるから C2〜B4 だけ見ればいい」
    と考えて試したが、実曲では**明確に悪くなった**（ナイト・オブ・ナイツで
    コードの手応えの中央値 0.87→0.78、調の判定も Dm→Gm と外れた）。
    鍵盤の全域に和音が散っている編曲では、低音域だけでは決まらない。
    合成音では絞ったほうが良く見えたので、そちらだけを見て決めなくてよかった。
    """
    import librosa

    return librosa.feature.chroma_cqt(y=harmonic, sr=sr, hop_length=HOP)


def _scale_of(tonic: int, minor: bool) -> set[int]:
    steps = MINOR_SCALE if minor else MAJOR_SCALE
    return {(tonic + s) % 12 for s in steps}


def _chord_agreement(tonic: int, minor: bool, chords: list[Chord]) -> float:
    """その調のとき、コード進行のどれだけの時間が調内に収まるか。"""
    scale = _scale_of(tonic, minor)
    dominant = (tonic + 7) % 12 if minor else None
    inside = total = 0.0
    for chord in chords:
        if chord.root < 0:
            continue
        span = chord.end - chord.start
        total += span
        allowed = scale
        if dominant is not None and chord.root == dominant and chord.quality in ("", "7"):
            allowed = scale | {(tonic + LEADING_TONE) % 12}
        if all(pc in allowed for pc in chord.pitch_classes):
            inside += span
    return inside / total if total else 0.0


def estimate_key(chroma: np.ndarray, chords: list[Chord] | None = None) -> Key:
    """調を当てる。

    クロマ全体の傾きだけ（Krumhansl-Schmuckler）だと、**平行調と同主調を
    取り違える**。平行調（Dm と F）は構成音がまったく同じだし、同主調
    （G と Gm）も差は 3 音しかないので、統計だけでは決まらない。

    ``chords`` を渡すと、コード進行がその調にどれだけ収まるかも見る。
    Gm・Cm・B♭ が並んでいるなら G メジャーではなく G マイナー、という判断ができる。
    """
    profile = chroma.mean(axis=1)
    profile = profile - profile.mean()
    norm = np.linalg.norm(profile)
    if norm < 1e-9:
        # クロマに偏りが無い。コードが分かっていればそれだけで決める
        if not chords:
            return Key(tonic=0, minor=False, confidence=0.0)
        profile = np.zeros(12, dtype=np.float32)
    else:
        profile = profile / norm

    best = (0, False, -2.0)
    for minor, template in ((False, MAJOR_PROFILE), (True, MINOR_PROFILE)):
        centred = template - template.mean()
        centred = centred / np.linalg.norm(centred)
        for tonic in range(12):
            score = float(profile @ np.roll(centred, tonic))
            if chords:
                score += KEY_CHORD_WEIGHT * _chord_agreement(tonic, minor, chords)
            if score > best[2]:
                best = (tonic, minor, score)
    return Key(tonic=best[0], minor=best[1], confidence=best[2])


def _key_score_vector(profile: np.ndarray, chords: list[Chord] | None) -> np.ndarray:
    """24 通りの調それぞれの点数。並びは ``minor * 12 + tonic``。"""
    scores = np.zeros(24, dtype=np.float32)
    for minor, template in ((False, MAJOR_PROFILE), (True, MINOR_PROFILE)):
        centred = template - template.mean()
        centred = centred / np.linalg.norm(centred)
        for tonic in range(12):
            score = float(profile @ np.roll(centred, tonic))
            if chords:
                score += KEY_CHORD_WEIGHT * _chord_agreement(tonic, minor, chords)
            scores[int(minor) * 12 + tonic] = score
    return scores


def _centred_profile(chroma: np.ndarray) -> np.ndarray | None:
    profile = chroma.mean(axis=1)
    profile = profile - profile.mean()
    norm = np.linalg.norm(profile)
    return None if norm < 1e-9 else profile / norm


def estimate_keys(
    chroma: np.ndarray, sr: int, chords: list[Chord], duration: float
) -> list[KeySpan]:
    """区間ごとに調を出す。曲の途中の転調を追うため。

    曲全体で 1 つの調と決めつけると、転調する曲では**曲のどこかが必ず外れる**。
    調はコード推定の下駄にしか使っていないので、外れた調の下駄は
    そのままコードの誤りになる。

    窓ごとに出したあと、同じ調に留まりやすい重みで均す。転調はそう頻繁には
    起きないので、均さないと窓ごとにばらつく。
    """
    window = max(8.0, min(KEY_WINDOW, duration / 4.0))
    starts = np.arange(0.0, max(duration - window, 0.0) + KEY_HOP, KEY_HOP)
    if len(starts) == 0:
        starts = np.array([0.0])

    frames_per_second = sr / HOP
    rows: list[np.ndarray] = []
    for start in starts:
        end = min(start + window, duration)
        lo = int(start * frames_per_second)
        hi = max(lo + 1, int(end * frames_per_second))
        profile = _centred_profile(chroma[:, lo:hi])
        if profile is None:
            profile = np.zeros(12, dtype=np.float32)
        local = [c for c in chords if c.end > start and c.start < end]
        rows.append(_key_score_vector(profile, local))

    path = _viterbi(np.stack(rows), KEY_SELF_BONUS)

    spans: list[KeySpan] = []
    for i, state in enumerate(path):
        tonic, minor = int(state % 12), bool(state // 12)
        start = float(starts[i])
        end = float(starts[i + 1]) if i + 1 < len(starts) else duration
        key = Key(tonic=tonic, minor=minor, confidence=float(rows[i][state]))
        if spans and (spans[-1].key.tonic, spans[-1].key.minor) == (tonic, minor):
            prev = spans[-1]
            better = prev.key if prev.key.confidence >= key.confidence else key
            spans[-1] = KeySpan(start=prev.start, end=end, key=better)
        else:
            spans.append(KeySpan(start=start, end=end, key=key))
    if spans:
        spans[0] = KeySpan(start=0.0, end=spans[0].end, key=spans[0].key)
        spans[-1] = KeySpan(start=spans[-1].start, end=duration, key=spans[-1].key)
    return spans


def main_key(spans: list[KeySpan]) -> Key | None:
    """一番長く続いた調。表示や、転調を追わない相手に渡すため。"""
    if not spans:
        return None
    return max(spans, key=lambda s: s.end - s.start).key


# --------------------------------------------------------------------------- #
# コード
# --------------------------------------------------------------------------- #


def _chord_templates(key: Key | None) -> tuple[np.ndarray, list[tuple[int, str]], np.ndarray]:
    """テンプレート行列・その (根音, 種類)・各コードへの下駄を返す。

    テンプレートは**平均を引いてある**。密な曲ではクロマがほぼ平坦になり、
    素のコサイン類似度では「構成音が多くて平坦なテンプレート」が勝ってしまう。
    平均を引くと「そのコードの音が平均よりどれだけ突き出ているか」を測る
    ことになり、密度に振り回されなくなる。
    """
    rows: list[np.ndarray] = []
    labels: list[tuple[int, str]] = []
    prior: list[float] = []
    scale = set(key.scale) if key else set()
    dominant = (key.tonic + 7) % 12 if key and key.minor else None

    for quality, (intervals, penalty) in CHORD_TEMPLATES.items():
        for root in range(12):
            vec = np.zeros(12, dtype=np.float32)
            for i, interval in enumerate(intervals):
                # 根音・第3音・第5音が本体。7th はそれより軽く見る
                vec[(root + interval) % 12] = 1.0 if i < 3 else 0.6
            vec -= vec.mean()
            rows.append(vec / np.linalg.norm(vec))
            labels.append((root, quality))

            bonus = -penalty
            if scale:
                allowed = set(scale)
                # 短調の V（長三和音・属七）だけは導音を調内とみなす
                if dominant is not None and root == dominant and quality in ("", "7"):
                    allowed.add((key.tonic + LEADING_TONE) % 12)
                pcs = [(root + i) % 12 for i in intervals]
                bonus += DIATONIC_BONUS * sum(p in allowed for p in pcs) / len(pcs)
            prior.append(bonus)

    return np.stack(rows), labels, np.array(prior, dtype=np.float32)


def _viterbi(scores: np.ndarray, self_bonus: float) -> np.ndarray:
    """コードが頻繁に飛ばないよう、時間方向に均す。

    「同じコードに留まると得をする」だけの単純な遷移モデル。
    確率モデルを組むより、この形のほうが調整点が 1 つで済む。
    """
    n_frames, n_states = scores.shape
    best = scores[0].copy()
    back = np.zeros((n_frames, n_states), dtype=np.int32)

    for t in range(1, n_frames):
        stay = best + self_bonus
        jump_from = int(np.argmax(best))
        jump = best[jump_from]
        take_stay = stay >= jump
        back[t] = np.where(take_stay, np.arange(n_states), jump_from)
        best = np.where(take_stay, stay, jump) + scores[t]

    path = np.zeros(n_frames, dtype=np.int32)
    path[-1] = int(np.argmax(best))
    for t in range(n_frames - 1, 0, -1):
        path[t - 1] = back[t][path[t]]
    return path


def _segment_edges(beats: np.ndarray, n_segments: int, duration: float) -> list[float]:
    """拍から、区間の境目（秒）を作る。境目の数は区間の数 + 1。

    区間は音の頭から終わりまで隙間なく覆う。最初の拍は曲頭より後ろにあるので、
    そこを空けると冒頭が丸ごと「コードなし」になってしまう。
    """
    edges = [0.0]
    for t in beats:
        if float(t) > edges[-1]:
            edges.append(float(t))
    edges.append(max(duration, edges[-1]))
    # 想定と合わなければ、区間の数に合わせて詰める（ずらすよりは切るほうが安全）
    return edges[: n_segments + 1]


def estimate_chords(
    chroma: np.ndarray, beats: np.ndarray, sr: int, key: Key | list[KeySpan] | None
) -> list[Chord]:
    """拍ごとにコードを当てる。

    ``key`` に ``KeySpan`` の並びを渡すと、**拍ごとにその場所の調**で
    調内の下駄を掛ける。転調する曲ではこれが要る。
    """
    import librosa

    if len(beats) < 2:
        return []
    beat_frames = np.clip(
        librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, chroma.shape[1] - 1
    )

    synced = librosa.util.sync(chroma, beat_frames, aggregate=np.mean)
    synced = synced - synced.mean(axis=0, keepdims=True)
    synced = synced / np.maximum(np.linalg.norm(synced, axis=0, keepdims=True), 1e-9)

    # librosa.util.sync は境界の**あいだ**を区間にするので、最初の拍より前と
    # 最後の拍より後にも区間ができる。拍の数と区間の数は一致しない。
    # ここを取り違えると、全部のコードが 1 拍ぶんずれる。
    edges = _segment_edges(beats, synced.shape[1], chroma.shape[1] * HOP / sr)

    spans = key if isinstance(key, list) else None
    templates, labels, prior = _chord_templates(spans[0].key if spans else key)
    scores = templates @ synced                      # (状態数, 拍数)

    if spans:
        # 拍ごとに、その時刻の調の下駄を掛ける
        priors = {}
        for span in spans:
            ident = (span.key.tonic, span.key.minor)
            if ident not in priors:
                priors[ident] = _chord_templates(span.key)[2]
        def key_ident(at: float) -> tuple[int, bool]:
            for span in spans:
                if span.start <= at < span.end:
                    return span.key.tonic, span.key.minor
            return spans[-1].key.tonic, spans[-1].key.minor

        middles = [(edges[i] + edges[i + 1]) / 2 for i in range(len(edges) - 1)]
        scores = scores + np.stack([priors[key_ident(t)] for t in middles], axis=1)
    else:
        scores = scores + prior[:, None]

    # 「コードなし」も同じ土俵で競わせる。均したあとで足切りすると、
    # せっかく繋がったコードが N と交互になってちらつく。
    # 高さはこの曲自身の手応えから決める（曲をまたいで同じ意味になるように）
    no_chord = NO_CHORD_RATIO * float(np.median(scores.max(axis=0)))
    scores = np.vstack([scores, np.full((1, scores.shape[1]), no_chord, dtype=scores.dtype)])
    labels = labels + [(-1, "")]

    path = _viterbi(scores.T, CHORD_SELF_BONUS)

    chords: list[Chord] = []
    for i, state in enumerate(path):
        if i + 1 >= len(edges):
            break
        root, quality = labels[state]
        confidence = float(scores[state, i])
        chords.append(
            Chord(start=float(edges[i]), end=float(edges[i + 1]),
                  root=root, quality=quality, confidence=confidence)
        )

    return _merge_chords(chords)


def _merge_chords(chords: list[Chord]) -> list[Chord]:
    """同じコードが続く区間をひとつにまとめる。"""
    merged: list[Chord] = []
    for c in chords:
        if merged and merged[-1].root == c.root and merged[-1].quality == c.quality:
            prev = merged[-1]
            merged[-1] = Chord(
                start=prev.start, end=c.end, root=prev.root, quality=prev.quality,
                confidence=max(prev.confidence, c.confidence),
            )
        else:
            merged.append(c)
    return merged


# --------------------------------------------------------------------------- #
# 主旋律
# --------------------------------------------------------------------------- #


def _salience(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """半音ごとの「その音が鳴っている手応え」を返す。

    CQT の大きさそのままだと倍音が基音と見分けられない。基音の位置に
    倍音の分を足し込むことで、基音のほうが手応えが大きくなるようにする。
    """
    import librosa

    n_bins = MELODY_HI_MIDI - MELODY_LO_MIDI + 1
    fmin = float(librosa.midi_to_hz(MELODY_LO_MIDI))
    # 倍音を足すぶん上に余分に取るが、ナイキスト周波数は超えられない
    headroom = int(np.floor(12 * np.log2(sr / 2 / fmin))) - n_bins
    cqt = np.abs(
        librosa.cqt(
            y, sr=sr, hop_length=HOP, fmin=fmin,
            n_bins=n_bins + max(0, headroom), bins_per_octave=12,
        )
    )
    salience = np.zeros((n_bins, cqt.shape[1]), dtype=np.float32)
    for h, weight in enumerate(HARMONIC_WEIGHTS, start=1):
        shift = int(round(12 * np.log2(h)))
        if shift >= cqt.shape[0]:
            break
        take = cqt[shift : shift + n_bins]
        salience[: take.shape[0]] += weight * take
    pitches = np.arange(MELODY_LO_MIDI, MELODY_HI_MIDI + 1, dtype=np.float32)
    return salience, pitches


def extract_melody(
    y: np.ndarray, sr: int, high_bias: float | None = None
) -> list[tuple[float, float]]:
    """主旋律らしい単旋律を取り出す。

    多声の中から旋律を選ぶので、各フレームで一番強い音を取るだけでは
    低音（たいてい一番大きい）に引きずられ、しかも音がぶつ切りになる。
    そこで **滑らかに繋がること** を条件に、全体で一番よい経路を選ぶ。

    ``high_bias`` は高い声部をどれだけ贔屓するか。単声の音源を渡すときは
    ``MELODY_HIGH_BIAS_MONO``（＝0）にすること。既定は多声向け。
    """
    if high_bias is None:
        high_bias = MELODY_HIGH_BIAS
    import librosa

    salience, pitches = _salience(y, sr)
    peak = float(salience.max())
    if peak <= 0:
        return []

    # dB にして「全体最大からどれだけ下か」に直す
    db = 20.0 * np.log10(np.maximum(salience, 1e-9) / peak)
    emission = np.clip(db, MELODY_FLOOR_DB * 2, 0.0) / -MELODY_FLOOR_DB  # おおむね -2..0

    # 主旋律は上の声部にあることが多いので、高いほうに軽く下駄を履かせる
    if high_bias:
        high = (pitches - pitches[0]) / max(pitches[-1] - pitches[0], 1)
        emission += high_bias * high[:, None]

    n_pitch, n_frames = emission.shape
    # 最後の状態が「鳴っていない」。emission は 0（＝床すれすれ）を割り当てる
    emit = np.vstack([emission, np.full((1, n_frames), -1.0, dtype=np.float32)])

    jump = np.abs(pitches[:, None] - pitches[None, :])
    trans = np.zeros((n_pitch + 1, n_pitch + 1), dtype=np.float32)
    trans[:n_pitch, :n_pitch] = -MELODY_JUMP_PENALTY * jump
    trans[n_pitch, :n_pitch] = -MELODY_VOICING_PENALTY
    trans[:n_pitch, n_pitch] = -MELODY_VOICING_PENALTY

    best = emit[:, 0].copy()
    back = np.zeros((n_frames, n_pitch + 1), dtype=np.int32)
    for t in range(1, n_frames):
        total = best[:, None] + trans
        back[t] = np.argmax(total, axis=0)
        best = total[back[t], np.arange(n_pitch + 1)] + emit[:, t]

    path = np.zeros(n_frames, dtype=np.int32)
    path[-1] = int(np.argmax(best))
    for t in range(n_frames - 1, 0, -1):
        path[t - 1] = back[t][path[t]]

    times = librosa.frames_to_time(np.arange(n_frames), sr=sr, hop_length=HOP)
    return [
        (float(times[t]), float(pitches[state]))
        for t, state in enumerate(path)
        if state < n_pitch
    ]


# --------------------------------------------------------------------------- #
# まとめて
# --------------------------------------------------------------------------- #


def _load_stems(path: Path, duration: float | None, say) -> tuple[np.ndarray | None, Path | None]:
    """声と伴奏に分ける。分けられなければ ``(None, None)``。

    声トラックがほぼ無音なら「ボーカル無し」とみなして ``None`` を返す。
    インスト曲に対して分離の残りかすを主旋律として追いかけても意味がない。
    """
    import librosa

    from . import stems as stems_mod

    try:
        result = stems_mod.separate(path, verbose=False)
    except stems_mod.StemError as e:
        say(f"分離できませんでした（混ざったまま進みます）: {e}")
        return None, None

    vocals, _ = librosa.load(str(result.vocals), sr=ANALYSIS_RATE, mono=True, duration=duration)
    mix, _ = librosa.load(str(path), sr=ANALYSIS_RATE, mono=True, duration=duration)
    level = float(np.sqrt(np.mean(vocals**2))) / max(float(np.sqrt(np.mean(mix**2))), 1e-9)
    if level < VOCAL_PRESENCE_RATIO:
        say(f"ボーカルは入っていないようです（声の音量比 {level:.1%}）")
        return None, result.instrumental
    say(f"ボーカルを分離しました（声の音量比 {level:.0%}）")
    return vocals, result.instrumental


def analyze(
    path: Path | str,
    duration: float | None = None,
    melody: bool = True,
    stems: bool = True,
    verbose: bool = False,
) -> MusicalContext:
    """原音を解析して文脈を返す。

    ``stems=True`` なら声と伴奏に分けてから解析する。歌ものでは
    **混ざったままだと主旋律が伴奏を追いかけてしまう**ので、これが要る。
    """
    import librosa

    def say(message: str) -> None:
        if verbose:
            print(f"  {message}", flush=True)

    path = Path(path)
    say("読み込み中…")
    y, sr = librosa.load(str(path), sr=ANALYSIS_RATE, mono=True, duration=duration)

    vocals: np.ndarray | None = None
    instrumental: Path | None = None
    if stems and melody:
        say("声と伴奏に分けています…")
        vocals, instrumental = _load_stems(path, duration, say)

    # 拍は原音から取る。分離すると打楽器が痩せて、かえって取りにくくなる
    say("拍を取っています…")
    tempo, beat_frames = track_beats(y, sr)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)

    say("コードを推定しています…")
    harmonic = separate_harmonic(y)
    chroma = chord_chroma(harmonic, sr)

    # 一度コードを出してから、それを証拠にして調を決め直す。
    # 調が変わればコードの下駄も変わるので、もう一度だけ回す。
    duration = len(y) / sr
    chords = estimate_chords(chroma, beats, sr, estimate_key(chroma))
    spans = estimate_keys(chroma, sr, chords, duration)
    key = main_key(spans)
    if len(spans) > 1:
        say("転調: " + " → ".join(f"{s.key.name}({s.start:.0f}s)" for s in spans))
    chords = estimate_chords(chroma, beats, sr, spans)

    # 拍子はコードの変わり目を手がかりにするので、コードの後で決める
    downbeat_idx, beats_per_bar, stability = find_downbeats(y, sr, beat_frames, chords)
    if stability >= METER_STABLE:
        say(f"{beats_per_bar}拍子とみなします（窓ごとの一致率 {stability:.0%}）")
    else:
        say(f"拍子が一定でありません（一致率 {stability:.0%}）— 小節は使いません")

    melody_line: list[tuple[float, float]] = []
    melody_source = "mix"
    if melody:
        if vocals is not None:
            say("主旋律を追っています（分離した声から）…")
            # 単声なので高音への下駄は要らない。付けるとオクターブずれる
            melody_line = extract_melody(vocals, sr, high_bias=MELODY_HIGH_BIAS_MONO)
            melody_source = "vocals"
        else:
            say("主旋律を追っています…")
            melody_line = extract_melody(harmonic, sr)

    return MusicalContext(
        tempo=tempo,
        beats=[float(t) for t in beats],
        downbeats=[float(beats[i]) for i in downbeat_idx if i < len(beats)],
        beats_per_bar=beats_per_bar,
        meter_stability=stability,
        key=key,
        keys=spans,
        chords=chords,
        melody=melody_line,
        duration=duration,
        melody_source=melody_source,
        instrumental=str(instrumental) if instrumental else None,
    )


#: 解析結果の置き場。曲ごとに数分かかるので使い回す
ANALYSIS_CACHE = Path("cache/analysis")


def analyze_cached(
    path: Path | str,
    duration: float | None = None,
    cache_dir: Path | None = None,
    verbose: bool = False,
) -> MusicalContext:
    """解析するが、前に解析してあればそれを読む。

    分離と CQT で数分かかるので、同じ曲を作り直すたびに待つのは無駄。
    """
    import json

    path = Path(path)
    cache_dir = Path(cache_dir or ANALYSIS_CACHE)
    tag = path.stem + (f"_{duration:g}s" if duration else "")
    cached = cache_dir / f"{tag}.json"

    if cached.is_file():
        try:
            context = MusicalContext.from_dict(json.loads(cached.read_text(encoding="utf-8")))
            if verbose:
                print(f"  解析済みを使います: {cached}", flush=True)
            return context
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            if verbose:
                print(f"  解析済みを使えません（{e}）。やり直します", flush=True)

    context = analyze(path, duration=duration, verbose=verbose)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(context.to_dict(), ensure_ascii=False), encoding="utf-8")
    return context


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="原音から拍・調・コード・主旋律を取り出す")
    ap.add_argument("audio")
    ap.add_argument("--duration", type=float, default=None, help="先頭の秒数だけ解析する")
    ap.add_argument("--no-melody", action="store_true", help="主旋律の抽出を省く（速い）")
    ap.add_argument("--no-stems", action="store_true", help="声と伴奏に分けずに解析する")
    ap.add_argument("--json", type=Path, default=None, help="結果を JSON に保存")
    args = ap.parse_args(argv)

    context = analyze(args.audio, duration=args.duration, melody=not args.no_melody,
                      stems=not args.no_stems, verbose=True)
    print()
    print(context.summary())

    if args.json:
        import json

        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(context.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\n保存: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
