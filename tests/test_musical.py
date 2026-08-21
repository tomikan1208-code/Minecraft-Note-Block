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
