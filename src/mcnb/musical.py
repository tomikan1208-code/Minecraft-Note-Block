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
#: これ未満の相関なら「コードなし」とみなす。はっきりしない箇所に
#: 無理やり名前を付けるより、付けないほうが後段で害が少ない。
NO_CHORD_THRESHOLD = 0.55

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
#: 高い声部を主旋律とみなす度合い。0 で音域による贔屓なし
MELODY_HIGH_BIAS = 0.35


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
    chords: list[Chord] = field(default_factory=list)
    #: 主旋律。(秒, MIDIノート番号)。鳴っていない区間は入らない
    melody: list[tuple[float, float]] = field(default_factory=list)
    duration: float = 0.0

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

    def beat_index(self, seconds: float) -> int:
        """その時刻が何拍目か。拍より前なら -1。"""
        return int(np.searchsorted(self.beats, seconds, side="right")) - 1

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
                f"テンポ  : {self.tempo:.1f} BPM  拍 {len(self.beats)} 個 / 小節頭 {len(self.downbeats)} 個",
                f"調      : {self.key.name if self.key else '不明'}"
                + (f"  (確信度 {self.key.confidence:.2f})" if self.key else ""),
                f"コード  : {len(self.chords)} 区間 / 異なり {len(set(c.name for c in self.chords))} 種",
                f"  進行  : {' → '.join(uniq[:20])}" + (" …" if len(uniq) > 20 else ""),
                f"主旋律  : {melody_seconds:.1f} 秒ぶん ({share:.0f}%)"
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
            "tempo": self.tempo,
            "duration": self.duration,
            "beats": self.beats,
            "downbeats": self.downbeats,
            "key": ({**asdict(self.key), "name": self.key.name} if self.key else None),
            "chords": [{**asdict(c), "name": c.name} for c in self.chords],
            "melody": [[t, p] for t, p in self.melody],
        }


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


def find_downbeats(y: np.ndarray, sr: int, beat_frames: np.ndarray, per_bar: int = 4) -> list[int]:
    """小節頭がどの拍かを返す（拍の番号）。

    拍子推定まではやらない。4拍子と仮定して、**どの位相に置くと
    その拍のオンセットが一番強いか**だけを選ぶ。
    """
    import librosa

    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP, aggregate=np.median)
    strength = onset[np.clip(beat_frames, 0, len(onset) - 1)]
    if len(strength) < per_bar:
        return list(range(len(strength)))
    phase = max(range(per_bar), key=lambda p: float(np.mean(strength[p::per_bar])))
    return list(range(phase, len(beat_frames), per_bar))


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


def estimate_key(chroma: np.ndarray) -> Key:
    """クロマ全体の傾きから調を当てる（Krumhansl-Schmuckler）。"""
    profile = chroma.mean(axis=1)
    profile = profile - profile.mean()
    norm = np.linalg.norm(profile)
    if norm < 1e-9:
        return Key(tonic=0, minor=False, confidence=0.0)
    profile = profile / norm

    best = (0, False, -2.0)
    for minor, template in ((False, MAJOR_PROFILE), (True, MINOR_PROFILE)):
        centred = template - template.mean()
        centred = centred / np.linalg.norm(centred)
        for tonic in range(12):
            score = float(profile @ np.roll(centred, tonic))
            if score > best[2]:
                best = (tonic, minor, score)
    return Key(tonic=best[0], minor=best[1], confidence=best[2])


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


def estimate_chords(chroma: np.ndarray, beats: np.ndarray, sr: int, key: Key | None) -> list[Chord]:
    """拍ごとにコードを当てる。"""
    import librosa

    if len(beats) < 2:
        return []
    beat_frames = np.clip(
        librosa.time_to_frames(beats, sr=sr, hop_length=HOP), 0, chroma.shape[1] - 1
    )

    synced = librosa.util.sync(chroma, beat_frames, aggregate=np.mean)
    synced = synced - synced.mean(axis=0, keepdims=True)
    synced = synced / np.maximum(np.linalg.norm(synced, axis=0, keepdims=True), 1e-9)

    templates, labels, prior = _chord_templates(key)
    scores = templates @ synced + prior[:, None]     # (状態数, 拍数)

    # 「コードなし」も同じ土俵で競わせる。均したあとで足切りすると、
    # せっかく繋がったコードが N と交互になってちらつく
    scores = np.vstack([scores, np.full((1, scores.shape[1]), NO_CHORD_THRESHOLD, dtype=scores.dtype)])
    labels = labels + [(-1, "")]

    path = _viterbi(scores.T, CHORD_SELF_BONUS)

    # 区間は音の頭から終わりまで隙間なく覆う。最初の拍は曲頭より後ろにあるので、
    # そのままだと冒頭が「コードなし」になってしまう
    edges = [float(t) for t in beats] + [chroma.shape[1] * HOP / sr]
    edges[0] = 0.0
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


def extract_melody(y: np.ndarray, sr: int) -> list[tuple[float, float]]:
    """主旋律らしい単旋律を取り出す。

    多声の中から旋律を選ぶので、各フレームで一番強い音を取るだけでは
    低音（たいてい一番大きい）に引きずられ、しかも音がぶつ切りになる。
    そこで **滑らかに繋がること** を条件に、全体で一番よい経路を選ぶ。
    """
    import librosa

    salience, pitches = _salience(y, sr)
    peak = float(salience.max())
    if peak <= 0:
        return []

    # dB にして「全体最大からどれだけ下か」に直す
    db = 20.0 * np.log10(np.maximum(salience, 1e-9) / peak)
    emission = np.clip(db, MELODY_FLOOR_DB * 2, 0.0) / -MELODY_FLOOR_DB  # おおむね -2..0

    # 主旋律は上の声部にあることが多いので、高いほうに軽く下駄を履かせる
    high = (pitches - pitches[0]) / max(pitches[-1] - pitches[0], 1)
    emission += MELODY_HIGH_BIAS * high[:, None]

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


def analyze(
    path: Path | str,
    duration: float | None = None,
    melody: bool = True,
    verbose: bool = False,
) -> MusicalContext:
    """原音を解析して文脈を返す。"""
    import librosa

    def say(message: str) -> None:
        if verbose:
            print(f"  {message}", flush=True)

    say("読み込み中…")
    y, sr = librosa.load(str(path), sr=ANALYSIS_RATE, mono=True, duration=duration)

    say("拍を取っています…")
    tempo, beat_frames = track_beats(y, sr)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=HOP)
    downbeat_idx = find_downbeats(y, sr, beat_frames)

    say("コードを推定しています…")
    harmonic = separate_harmonic(y)
    chroma = chord_chroma(harmonic, sr)
    key = estimate_key(chroma)
    chords = estimate_chords(chroma, beats, sr, key)

    melody_line: list[tuple[float, float]] = []
    if melody:
        say("主旋律を追っています…")
        melody_line = extract_melody(harmonic, sr)

    return MusicalContext(
        tempo=tempo,
        beats=[float(t) for t in beats],
        downbeats=[float(beats[i]) for i in downbeat_idx if i < len(beats)],
        key=key,
        chords=chords,
        melody=melody_line,
        duration=len(y) / sr,
    )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="原音から拍・調・コード・主旋律を取り出す")
    ap.add_argument("audio")
    ap.add_argument("--duration", type=float, default=None, help="先頭の秒数だけ解析する")
    ap.add_argument("--no-melody", action="store_true", help="主旋律の抽出を省く（速い）")
    ap.add_argument("--json", type=Path, default=None, help="結果を JSON に保存")
    args = ap.parse_args(argv)

    context = analyze(args.audio, duration=args.duration, melody=not args.no_melody, verbose=True)
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
