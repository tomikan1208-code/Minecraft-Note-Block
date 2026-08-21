"""musical.py の検証 — 答えの分かっている音を合成して、当てられるか見る。

コード推定や主旋律抽出が合っているかは、本来は聴いて確かめるもの。
ただし「聴いて確かめる」は自動では回せないし、直したときに壊れても気づけない。

そこで **こちらで作った音** を食わせる。テンポも調もコードも主旋律も
分かっているので、答え合わせができる。実曲で合うかは別問題だが、
少なくとも「壊れていない」ことは機械的に確かめられる。
"""

from __future__ import annotations

import numpy as np
import pytest

from mcnb import musical as M

SR = M.ANALYSIS_RATE
TEMPO = 120.0
BEAT = 60.0 / TEMPO
BARS = 8

#: 答え。D マイナーを 1 小節ずつ。
#: Dm→B♭→F→C のような進行は F メジャー（vi→IV→I→V）とも読めてしまい、
#: 相対調のどちらが正解か決まらない。i→iv→V→i にすると、V の長三和音が
#: 導音 C# を持ち込むので D マイナーで確定する。
PROGRESSION = [(2, "m"), (7, "m"), (9, ""), (2, "m")]   # Dm → Gm → A → Dm
#: 主旋律。小節ごとに 4 分音符 4 つ。
#: V（A メジャー）の小節では導音 C# を使う。ここで C ナチュラルを鳴らすと
#: 音源そのものが A メジャーと矛盾し、Am と判定されるのが正しくなってしまう。
MELODY_BARS = [
    [74, 72, 70, 69],   # Dm : D5  C5  A#4 A4
    [70, 69, 67, 65],   # Gm : A#4 A4  G4  F4
    [69, 73, 76, 73],   # A  : A4  C#5 E5  C#5
    [74, 72, 69, 74],   # Dm : D5  C5  A4  D5
    [77, 74, 72, 70],   # Dm : F5  D5  C5  A#4
    [70, 69, 67, 65],   # Gm : A#4 A4  G4  F4
    [73, 76, 81, 76],   # A  : C#5 E5  A5  E5
    [74, 70, 69, 62],   # Dm : D5  A#4 A4  D4
]
MELODY_MIDI = [note for bar in MELODY_BARS for note in bar]


def _voice(midi: float, length: int, harmonics: tuple[float, ...]) -> np.ndarray:
    """1 声ぶんの音。正弦だけだと現実離れするので倍音を足す。"""
    t = np.arange(length) / SR
    freq = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
    wave = np.zeros(length, dtype=np.float32)
    for h, gain in enumerate(harmonics, start=1):
        wave += gain * np.sin(2.0 * np.pi * freq * h * t)
    # 打鍵らしい減衰。持続音のままだとオンセットが立たず拍が取れない
    return wave * np.exp(-t * 3.0).astype(np.float32)


def synth() -> tuple[np.ndarray, list[tuple[float, float, int, str]], list[tuple[float, int]]]:
    """答えの分かっている音を作る。

    返すのは (音, コードの正解, 主旋律の正解)。
    """
    total = int(BARS * 4 * BEAT * SR) + SR
    audio = np.zeros(total, dtype=np.float32)
    chord_truth: list[tuple[float, float, int, str]] = []
    melody_truth: list[tuple[float, int]] = []

    for bar in range(BARS):
        root, quality = PROGRESSION[bar % len(PROGRESSION)]
        intervals = M.CHORD_TEMPLATES[quality][0]
        bar_start = bar * 4 * BEAT
        chord_truth.append((bar_start, bar_start + 4 * BEAT, root, quality))

        for beat in range(4):
            at = int((bar_start + beat * BEAT) * SR)
            length = int(BEAT * SR)

            # 伴奏: 構成音を C3〜B3 に置く
            for interval in intervals:
                midi = 48 + (root + interval) % 12
                audio[at : at + length] += 0.20 * _voice(midi, length, (1.0, 0.4, 0.2))
            # ベース: 根音の 2 オクターブ下
            audio[at : at + length] += 0.25 * _voice(36 + root, length, (1.0, 0.5, 0.25))

            # 主旋律。伴奏より大きくして、上の声部に置く
            note = MELODY_MIDI[(bar * 4 + beat) % len(MELODY_MIDI)]
            audio[at : at + length] += 0.55 * _voice(note, length, (1.0, 0.5, 0.3, 0.15))
            melody_truth.append((bar_start + beat * BEAT, note))

    audio += np.random.default_rng(0).normal(0, 0.002, total).astype(np.float32)
    return audio / np.max(np.abs(audio)) * 0.9, chord_truth, melody_truth


@pytest.fixture(scope="module")
def analysed():
    audio, chord_truth, melody_truth = synth()
    import librosa

    tempo, beat_frames = M.track_beats(audio, SR)
    beats = librosa.frames_to_time(beat_frames, sr=SR, hop_length=M.HOP)
    harmonic = M.separate_harmonic(audio)
    chroma = M.chord_chroma(harmonic, SR)
    key = M.estimate_key(chroma)
    chords = M.estimate_chords(chroma, beats, SR, key)
    melody = M.extract_melody(harmonic, SR)
    return {
        "tempo": tempo, "key": key, "chords": chords, "melody": melody,
        "chord_truth": chord_truth, "melody_truth": melody_truth,
        "duration": len(audio) / SR,
    }


def test_tempo(analysed):
    """テンポを当てる。倍・半分に落ちていないこと。"""
    assert analysed["tempo"] == pytest.approx(TEMPO, rel=0.06)


def test_key(analysed):
    """調は D マイナー。"""
    key = analysed["key"]
    assert (key.tonic, key.minor) == (2, True), f"{key.name} と判定された"


def test_chords(analysed):
    """コードを時間の 9 割近くで当てる。

    残りはほぼコードの変わり目の 1 拍ぶんで、拍単位で推定している以上の
    分解能は出ない。
    """
    chords = analysed["chords"]
    step = 0.05
    hit = total = 0
    for start, end, root, quality in analysed["chord_truth"]:
        for t in np.arange(start + 0.1, end - 0.1, step):
            got = next((c for c in chords if c.start <= t < c.end), None)
            total += 1
            if got and got.root == root and got.quality == quality:
                hit += 1
    ratio = hit / max(total, 1)
    assert ratio >= 0.88, f"一致 {ratio:.0%}（{hit}/{total}）"


def test_chord_tones_even_when_quality_differs(analysed):
    """種類まで当たらなくても、構成音は当たっていること。

    Dm を Dm7 と読むのは編曲上ほぼ無害だが、Dm を G と読むのは害がある。
    """
    chords = analysed["chords"]
    hit = total = 0
    for start, end, root, quality in analysed["chord_truth"]:
        truth = {(root + i) % 12 for i in M.CHORD_TEMPLATES[quality][0]}
        for t in np.arange(start + 0.1, end - 0.1, 0.05):
            got = next((c for c in chords if c.start <= t < c.end), None)
            total += 1
            if got and got.root >= 0:
                overlap = len(truth & set(got.pitch_classes)) / len(truth)
                hit += overlap
    ratio = hit / max(total, 1)
    assert ratio >= 0.92, f"構成音の一致 {ratio:.0%}"


def test_melody(analysed):
    """主旋律を、時間の 7 割以上で半音以内に当てる。"""
    melody = analysed["melody"]
    assert melody, "主旋律がまったく取れていない"

    lookup = {round(t, 3): p for t, p in melody}
    times = np.array(sorted(lookup))
    hit = total = 0
    for start, note in analysed["melody_truth"]:
        # 打鍵直後は前の音の余韻が残るので、音符の真ん中あたりで見る
        for t in (start + BEAT * 0.35, start + BEAT * 0.6):
            total += 1
            i = int(np.searchsorted(times, t))
            for j in (i - 1, i):
                if 0 <= j < len(times) and abs(times[j] - t) < 0.05:
                    if abs(lookup[times[j]] - note) <= 1.0:
                        hit += 1
                    break
    ratio = hit / max(total, 1)
    assert ratio >= 0.70, f"一致 {ratio:.0%}（{hit}/{total}）"


def test_melody_is_not_the_bass(analysed):
    """低音を主旋律と間違えていないこと。

    各フレームで一番強い音を取るだけだと、たいてい一番大きいベースを拾う。
    そうなっていないかを、平均音高で見る。
    """
    melody = analysed["melody"]
    mean_pitch = float(np.mean([p for _, p in melody]))
    bass_mean = float(np.mean([36 + r for r, _ in PROGRESSION]))
    assert mean_pitch > bass_mean + 12, f"平均 {mean_pitch:.1f} — ベース({bass_mean:.1f})寄り"


def test_chord_lookup_matches_linear_scan(analysed):
    """chord_at() の二分探索が、素直に走査した結果と一致すること。"""
    context = M.MusicalContext(tempo=analysed["tempo"], chords=analysed["chords"])
    for t in np.arange(0.0, analysed["duration"], 0.037):
        expected = next((c for c in analysed["chords"] if c.start <= t < c.end), None)
        assert context.chord_at(float(t)) is expected, f"{t:.3f}s"


# --------------------------------------------------------------------------- #
# 曲をまたいでも同じ意味になるか
# --------------------------------------------------------------------------- #


def _muddy(audio: np.ndarray) -> np.ndarray:
    """和音の手応えを落とした版を作る。

    実曲では編成や音の混み具合で、クロマとコードの相関の高さがそもそも違う。
    真っ黒ピアノでは中央値 0.73、歌ものでは 0.62 だった。しきい値を絶対値で
    決めると、この差だけで「コードなし」の割合が 6% と 31% に割れてしまう。
    """
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1, len(audio)).astype(np.float32)
    # 低域寄りの雑音にする。白色雑音はクロマにあまり効かない
    noise = np.convolve(noise, np.ones(64, dtype=np.float32) / 64, mode="same")
    return audio * 0.7 + noise / max(np.max(np.abs(noise)), 1e-9) * 0.3


def _no_chord_share(audio: np.ndarray) -> tuple[float, float]:
    """(コードなしの時間の割合, 手応えの中央値) を返す。"""
    import librosa

    _, beat_frames = M.track_beats(audio, SR)
    beats = librosa.frames_to_time(beat_frames, sr=SR, hop_length=M.HOP)
    chroma = M.chord_chroma(M.separate_harmonic(audio), SR)
    key = M.estimate_key(chroma)
    chords = M.estimate_chords(chroma, beats, SR, key)

    total = sum(c.end - c.start for c in chords)
    none = sum(c.end - c.start for c in chords if c.root < 0)
    clarity = float(np.median([c.confidence for c in chords if c.root >= 0] or [0.0]))
    return none / max(total, 1e-9), clarity


def test_no_chord_share_survives_a_muddier_mix():
    """和音がぼやけた音源でも「コードなし」だらけにならないこと。

    ここが絶対値のしきい値だと、手応えが下がっただけで全部 N に落ちる。
    曲ごとの手応えに対する比で決めているので、割合は保たれるはず。
    """
    audio, _, _ = synth()
    clean_share, clean_clarity = _no_chord_share(audio)
    muddy_share, muddy_clarity = _no_chord_share(_muddy(audio))

    assert muddy_clarity < clean_clarity, (
        f"濁らせたのに手応えが落ちていない（{clean_clarity:.2f} → {muddy_clarity:.2f}）— 前提が崩れている"
    )
    assert muddy_share <= 0.25, f"濁らせたら N が {muddy_share:.0%} まで増えた"


def test_key_uses_the_chord_progression_to_break_the_tie():
    """平行調・同主調はクロマだけでは決まらないので、コード進行で決めること。

    Cm / Fm / G / Cm は C マイナー。構成音の統計だけでは E♭ メジャーとも
    C メジャーとも読めてしまう。
    """
    flat = np.ones((12, 8), dtype=np.float32)   # 偏りなし＝クロマからは何も分からない
    progression = [
        M.Chord(start=i * 2.0, end=i * 2.0 + 2.0, root=root, quality=quality, confidence=1.0)
        for i, (root, quality) in enumerate([(0, "m"), (5, "m"), (7, ""), (0, "m")])
    ]
    key = M.estimate_key(flat, progression)
    assert (key.tonic, key.minor) == (0, True), f"{key.name} と判定された"


# --------------------------------------------------------------------------- #
# 単声の音源（分離したボーカル）
# --------------------------------------------------------------------------- #


#: 基音が倍音より弱い声。ミックスで低域を削られた歌はこうなる。
#: 基音がしっかりしていれば下駄があっても倍音に負けないので、この条件でないと
#: 実際に起きたオクターブずれを再現できない。
THIN_VOICE = (0.3, 1.0, 0.8, 0.5, 0.3)


def _monophonic(notes: list[int], harmonics: tuple[float, ...] = THIN_VOICE) -> np.ndarray:
    """伴奏なしの単旋律。分離したボーカルの代わり。"""
    length = int(BEAT * SR)
    audio = np.zeros(length * len(notes), dtype=np.float32)
    for i, note in enumerate(notes):
        audio[i * length : (i + 1) * length] = _voice(note, length, harmonics)
    return audio / np.max(np.abs(audio)) * 0.9


def test_high_bias_causes_octave_errors_on_a_monophonic_source():
    """単声には高音への下駄を履かせないこと。

    下駄は多声の中で「一番大きいベース」を避けるためのもの。避けるべき低音が
    無い単声では、ただ倍音のほうへ引っぱるだけでオクターブずれになる。
    """
    notes = [60, 62, 64, 65, 67, 65, 64, 62, 60, 64, 67, 64]
    audio = _monophonic(notes)

    def octave_error(bias: float) -> float:
        melody = M.extract_melody(audio, SR, high_bias=bias)
        assert melody, f"下駄 {bias} で主旋律が取れなかった"
        wrong = 0
        for t, pitch in melody:
            want = notes[min(int(t / BEAT), len(notes) - 1)]
            if abs(pitch - want) > 1.5:
                wrong += 1
        return wrong / len(melody)

    with_bias = octave_error(M.MELODY_HIGH_BIAS)
    without = octave_error(M.MELODY_HIGH_BIAS_MONO)
    assert without < with_bias, f"下駄あり {with_bias:.0%} / なし {without:.0%} — 差が出ていない"
    assert without <= 0.15, f"下駄なしでも {without:.0%} 外している"


# --------------------------------------------------------------------------- #
# 平行調 — 構成音が同じなので、留まっている先で決めるしかない
# --------------------------------------------------------------------------- #

#: D マイナーだが、使う音は F メジャーとまったく同じ進行。
#: 統計では決まらない。Dm に留まっている時間が長いことだけが手がかり。
RELATIVE_PROGRESSION = [
    (2, "m"), (2, "m"), (10, ""), (5, ""),   # Dm Dm B♭ F
    (2, "m"), (2, "m"), (0, ""), (2, "m"),   # Dm Dm C  Dm
]
RELATIVE_MELODY = [
    [74, 72, 70, 69], [69, 70, 72, 74], [70, 69, 65, 62], [65, 69, 72, 69],
    [74, 72, 70, 69], [69, 72, 74, 77], [72, 76, 79, 76], [74, 69, 65, 62],
]


def _synth_progression(progression, melody_bars):
    """任意の進行で音を作る。"""
    total = int(len(progression) * 4 * BEAT * SR) + SR
    audio = np.zeros(total, dtype=np.float32)
    for bar, (root, quality) in enumerate(progression):
        intervals = M.CHORD_TEMPLATES[quality][0]
        for beat in range(4):
            at = int((bar * 4 * BEAT + beat * BEAT) * SR)
            length = int(BEAT * SR)
            for interval in intervals:
                audio[at:at + length] += 0.20 * _voice(48 + (root + interval) % 12, length, (1.0, 0.4, 0.2))
            audio[at:at + length] += 0.25 * _voice(36 + root, length, (1.0, 0.5, 0.25))
            note = melody_bars[bar % len(melody_bars)][beat]
            audio[at:at + length] += 0.55 * _voice(note, length, (1.0, 0.5, 0.3, 0.15))
    audio += np.random.default_rng(2).normal(0, 0.002, total).astype(np.float32)
    return audio / np.max(np.abs(audio)) * 0.9


def _key_of(audio):
    import librosa

    _, beat_frames = M.track_beats(audio, SR)
    beats = librosa.frames_to_time(beat_frames, sr=SR, hop_length=M.HOP)
    chroma = M.chord_chroma(M.separate_harmonic(audio), SR)
    key = M.estimate_key(chroma)
    chords = M.estimate_chords(chroma, beats, SR, key)
    return M.estimate_key(chroma, chords), chords


def test_relative_keys_are_decided_by_where_the_music_rests():
    """構成音が同じ平行調でも、D マイナーだと分かること。

    Dm・B♭・F・C は F メジャー（vi→IV→I→V）とも読める。
    """
    audio = _synth_progression(RELATIVE_PROGRESSION, RELATIVE_MELODY)
    key, _ = _key_of(audio)
    assert (key.tonic, key.minor) == (2, True), f"{key.name} と判定された"


# --------------------------------------------------------------------------- #
# 転調
# --------------------------------------------------------------------------- #

#: 前半 D マイナー、後半 F マイナー（短3度上）。よくある転調。
MODULATING_PROGRESSION = [
    (2, "m"), (7, "m"), (9, ""), (2, "m"),
    (2, "m"), (7, "m"), (9, ""), (2, "m"),
    (5, "m"), (10, "m"), (0, ""), (5, "m"),
    (5, "m"), (10, "m"), (0, ""), (5, "m"),
]
MODULATING_MELODY = (
    [[74, 72, 70, 69], [70, 69, 67, 65], [69, 73, 76, 73], [74, 72, 69, 74]] * 2
    + [[77, 75, 73, 72], [73, 72, 70, 68], [72, 76, 79, 76], [77, 75, 72, 77]] * 2
)


def _keys_of(audio):
    import librosa

    _, beat_frames = M.track_beats(audio, SR)
    beats = librosa.frames_to_time(beat_frames, sr=SR, hop_length=M.HOP)
    chroma = M.chord_chroma(M.separate_harmonic(audio), SR)
    chords = M.estimate_chords(chroma, beats, SR, M.estimate_key(chroma))
    return M.estimate_keys(chroma, SR, chords, len(audio) / SR)


def test_modulation_is_tracked():
    """曲の途中で転調したら、そこで調が変わること。

    曲全体で 1 つの調と決めつけると、転調する曲では**どこかが必ず外れる**。
    調はコード推定の下駄にしか使っていないので、外れた調の下駄は
    そのままコードの誤りになる。
    """
    audio = _synth_progression(MODULATING_PROGRESSION, MODULATING_MELODY)
    spans = _keys_of(audio)
    names = [s.key.name for s in spans]
    assert len(spans) >= 2, f"転調を見つけられていない: {names}"

    half = len(audio) / SR / 2
    first = next(s.key for s in spans if s.start <= half * 0.4 < s.end)
    second = next(s.key for s in spans if s.start <= half * 1.6 < s.end)
    assert (first.tonic, first.minor) == (2, True), f"前半が {first.name}"
    assert (second.tonic, second.minor) == (5, True), f"後半が {second.name}"


def test_a_single_key_piece_is_not_split():
    """転調していない曲を、勝手に転調したことにしないこと。

    追従を速くしすぎると、部分的なコードの揺れまで転調と読んでしまう。
    """
    audio, _, _ = synth()
    spans = _keys_of(audio)
    names = [s.key.name for s in spans]
    assert len(spans) == 1, f"転調していないのに分かれた: {names}"
    assert (spans[0].key.tonic, spans[0].key.minor) == (2, True), f"{names}"


# --------------------------------------------------------------------------- #
# 拍子
# --------------------------------------------------------------------------- #


def _synth_meter(per_bar: int, bars: int = 12, accent: float = 2.2) -> np.ndarray:
    """指定した拍子の音を作る。小節頭を強く弾き、コードも小節ごとに変える。

    5拍子・7拍子も同じ作りで出せるので、変拍子の扱いをそのまま試せる。
    """
    progression = [(2, "m"), (7, "m"), (9, ""), (2, "m")]
    length = int(BEAT * SR)
    audio = np.zeros(length * per_bar * bars + SR, dtype=np.float32)
    for bar in range(bars):
        root, quality = progression[bar % len(progression)]
        for beat in range(per_bar):
            at = (bar * per_bar + beat) * length
            gain = accent if beat == 0 else 1.0
            for interval in M.CHORD_TEMPLATES[quality][0]:
                audio[at:at + length] += 0.20 * gain * _voice(
                    48 + (root + interval) % 12, length, (1.0, 0.4, 0.2)
                )
            audio[at:at + length] += 0.28 * gain * _voice(36 + root, length, (1.0, 0.5, 0.25))
            audio[at:at + length] += 0.45 * _voice(
                62 + (beat * 3) % 12, length, (1.0, 0.5, 0.3, 0.15)
            )
    audio += np.random.default_rng(4).normal(0, 0.002, len(audio)).astype(np.float32)
    return audio / np.max(np.abs(audio)) * 0.9


def _meter_of(audio: np.ndarray) -> tuple[int, int]:
    import librosa

    _, beat_frames = M.track_beats(audio, SR)
    beats = librosa.frames_to_time(beat_frames, sr=SR, hop_length=M.HOP)
    chroma = M.chord_chroma(M.separate_harmonic(audio), SR)
    chords = M.estimate_chords(chroma, beats, SR, M.estimate_key(chroma))
    onset = librosa.onset.onset_strength(y=audio, sr=SR, hop_length=M.HOP, aggregate=np.median)
    strength = onset[np.clip(beat_frames, 0, len(onset) - 1)]
    return M.estimate_meter(strength, beats, chords)


@pytest.mark.parametrize("per_bar", [3, 4])
def test_common_meters(per_bar):
    """3拍子と4拍子を取り違えないこと。

    小節頭の強さだけで決めると 4拍子は 2拍子でも説明できてしまうので、
    コードの変わり目の周期を併せて見ている。
    """
    got, _ = _meter_of(_synth_meter(per_bar))
    assert got == per_bar, f"{per_bar}拍子を {got}拍子と判定"


@pytest.mark.parametrize("per_bar", [5, 7, 8])
def test_odd_and_long_meters(per_bar):
    """変拍子（5拍子・7拍子）と 8拍子も取れること。

    小節ごとに長さが変わる本物の変拍子は別の話。ここで見ているのは
    「4拍子でない一定の拍子」を 4拍子に押し込めないこと。

    8 が要るのは、拍の推定が8分音符に乗ると 4/4 が 1小節 8拍になるから。
    候補を 7 で打ち切ると、その曲が 7拍子として溢れる。
    """
    got, _ = _meter_of(_synth_meter(per_bar))
    assert got == per_bar, f"{per_bar}拍子を {got}拍子と判定"


def test_metrical_position_is_consistent_with_the_beats():
    """小節内の位置が、小節頭で 0 に戻ること。"""
    audio = _synth_meter(4)
    import librosa

    _, beat_frames = M.track_beats(audio, SR)
    beats = librosa.frames_to_time(beat_frames, sr=SR, hop_length=M.HOP)
    downbeats, per_bar = M.find_downbeats(audio, SR, beat_frames)
    context = M.MusicalContext(
        tempo=TEMPO,
        beats=[float(t) for t in beats],
        downbeats=[float(beats[i]) for i in downbeats if i < len(beats)],
        beats_per_bar=per_bar,
        duration=len(audio) / SR,
    )
    for at in context.downbeats[1:-1]:
        position = context.metrical_position(at + 0.001)
        assert position is not None
        assert position[1] == pytest.approx(0.0, abs=0.1), f"{at:.2f}s で {position[1]:.2f}拍"
        assert context.is_downbeat(at + 0.001)

    # 小節のまんなかは小節頭ではない
    middle = context.downbeats[1] + (context.downbeats[2] - context.downbeats[1]) / 2
    assert not context.is_downbeat(middle)
