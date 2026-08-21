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
    """コードを時間の 7 割以上で当てる。"""
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
    assert ratio >= 0.80, f"一致 {ratio:.0%}（{hit}/{total}）"


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
    assert ratio >= 0.85, f"構成音の一致 {ratio:.0%}"


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
