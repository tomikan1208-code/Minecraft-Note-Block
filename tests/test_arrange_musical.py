"""編曲が「音楽的な役割」で音を選べているかの検証。

これまで、残す音は**音量だけ**で決めていた。大きい音ほど曲の骨格だという
当て推量で、静かな主旋律より賑やかな伴奏を残してしまう。

原音の解析（拍・調・コード・主旋律）が入ったので役割で決められるようになった。
ここで見張るのは「減らしても主旋律が残るか」。音数を合わせても旋律が消えたら、
曲として成り立たない。
"""

from __future__ import annotations

import collections

from mcnb import arrange as A
from mcnb.musical import Chord, MusicalContext
from mcnb.song import TICKS_PER_SECOND, NoteEvent, Song

#: C メジャー（C E G）が 4 秒鳴っていて、主旋律が C5 → E5 と動く
CHORD = Chord(start=0.0, end=4.0, root=0, quality="", confidence=1.0)


def _context(**overrides) -> MusicalContext:
    base = dict(
        tempo=120.0,
        beats=[i * 0.5 for i in range(16)],
        downbeats=[i * 2.0 for i in range(4)],
        chords=[CHORD],
        melody=[(t / 20, 72.0 if t < 40 else 76.0) for t in range(80)],
        duration=4.0,
        beats_per_bar=4,
        meter_stability=1.0,
    )
    base.update(overrides)
    return MusicalContext(**base)


def _note(tick: int, midi: int, velocity: float) -> NoteEvent:
    return NoteEvent(tick=tick, instrument="harp", midi=midi, velocity=velocity)


def test_quiet_melody_beats_loud_accompaniment():
    """静かな主旋律のほうが、大きい伴奏より大事だと判断すること。

    これができないと、減らしたときに真っ先に旋律が消える。
    """
    context = _context()
    melody = _note(0, 72, 0.25)      # C5、小さい。主旋律の上にある
    # E4。大きいがただの伴奏。C4 にすると主旋律の 1 オクターブ下になり、
    # 「旋律の重ね」として正当に加点されるので比較にならない
    accompaniment = _note(0, 64, 0.95)

    assert A.importance(melody, context) > A.importance(accompaniment, context)
    # 解析が無ければ従来どおり音量で決まる
    assert A.importance(melody, None) < A.importance(accompaniment, None)


def test_chord_tones_beat_off_chord_notes():
    """同じ音量なら、コードの構成音を残すこと。"""
    context = _context()
    inside = _note(0, 64, 0.5)    # E4 — C メジャーの第3音
    outside = _note(0, 66, 0.5)   # F#4 — コード外
    assert A.importance(inside, context) > A.importance(outside, context)


def test_the_fifth_is_the_first_chord_tone_to_go():
    """構成音のなかでは第5音から削ること。第5音は省いても和音は壊れない。"""
    context = _context()
    root = _note(0, 60, 0.5)
    third = _note(0, 64, 0.5)
    fifth = _note(0, 67, 0.5)
    assert A.importance(fifth, context) < A.importance(third, context)
    assert A.importance(fifth, context) < A.importance(root, context)


def test_downbeat_bonus_is_off_when_the_meter_is_unreliable():
    """拍子が当てにならない曲では、小節頭を優遇しないこと。

    変拍子で小節頭を主張すると、**でたらめな位置の音**を優遇することになる。
    """
    steady = _context()
    shaky = _context(meter_stability=0.2)
    on_downbeat = _note(0, 62, 0.5)      # コード外にして、下駄の差だけを見る

    assert A.importance(on_downbeat, steady) > A.importance(on_downbeat, shaky)
    assert A.importance(on_downbeat, shaky) == A.importance(
        _note(int(0.75 * TICKS_PER_SECOND), 62, 0.5), shaky
    )


def _melody_survival(song: Song, context: MusicalContext) -> float:
    want = {
        (e.tick, e.midi)
        for e in song.events
        if (m := context.melody_at(e.tick / TICKS_PER_SECOND)) is not None
        and abs(e.midi - m) <= A.MELODY_TOLERANCE
    }
    return len(want)


def test_capping_keeps_the_melody():
    """同時発音を絞っても、音量順より多く主旋律が残ること。

    旋律は音を変えながら進むものにしてある。同じ音を連打する素材だと
    thin_sustains が正しく間引いてしまい、取捨の良し悪しが測れない。
    """
    line = [72, 74, 76, 77, 79, 77, 76, 74, 72, 74, 76, 79, 81, 79, 77, 76]
    step = 4  # tick
    melody = [
        (tick / TICKS_PER_SECOND, float(line[min(tick // step, len(line) - 1)]))
        for tick in range(0, len(line) * step)
    ]
    context = _context(melody=melody, duration=len(line) * step / TICKS_PER_SECOND)

    events: list[NoteEvent] = []
    for index, pitch in enumerate(line):
        tick = index * step
        events.append(_note(tick, pitch, 0.2))                 # 主旋律。小さい
        for i, midi in enumerate((48, 55, 59, 64, 67, 71)):    # 賑やかな伴奏
            events.append(_note(tick, midi, 0.9 - i * 0.05))
    song = Song(name="t", events=events)
    total = _melody_survival(song, context)
    assert total == len(line), f"素材の時点で {total}/{len(line)}"

    by_volume, _ = A.arrange(song, A.ArrangeConfig(max_concurrent=3))
    by_music, _ = A.arrange(song, A.ArrangeConfig(max_concurrent=3, context=context))

    kept_volume = _melody_survival(by_volume, context)
    kept_music = _melody_survival(by_music, context)
    assert kept_music > kept_volume, f"音量順 {kept_volume}/{total} / 音楽的 {kept_music}/{total}"
    assert kept_music >= total * 0.8, f"主旋律が {kept_music}/{total} しか残っていない"


# --------------------------------------------------------------------------- #
# 役割ごとの音色と強弱
# --------------------------------------------------------------------------- #


def _mixed_song() -> Song:
    """主旋律（小さい）と伴奏（大きい）が同じ音色で重なっている曲。

    採譜はこうなりがち。原音の音色をなぞって楽器を選ぶので、旋律も伴奏も harp
    になり、しかも旋律のほうが音量が小さい。
    """
    line = [72, 74, 76, 77, 79, 77, 76, 74]
    events: list[NoteEvent] = []
    for index, pitch in enumerate(line):
        tick = index * 4
        events.append(_note(tick, pitch, 0.30))
        for midi in (55, 60, 64, 67):
            events.append(_note(tick, midi, 0.85))
    return Song(name="mixed", events=events)


def _melody_context(line=(72, 74, 76, 77, 79, 77, 76, 74), step: int = 4) -> MusicalContext:
    melody = [
        (tick / TICKS_PER_SECOND, float(line[min(tick // step, len(line) - 1)]))
        for tick in range(len(line) * step)
    ]
    return _context(melody=melody, duration=len(line) * step / TICKS_PER_SECOND)


def test_melody_gets_its_own_voice():
    """主旋律を伴奏と違う音色にすること。

    音符ブロックは1つの音色につき2オクターブしかない。同じ音色で重ねると、
    いくら音量を上げても旋律は伴奏に溶ける。
    """
    context = _melody_context()
    song = _mixed_song()
    out, _ = A.arrange(song, A.ArrangeConfig(context=context, max_concurrent=0))

    melody = [e for e in out.events if A.is_melody(e, context)]
    assert melody, "主旋律が残っていない"
    others = {e.instrument for e in out.events if not A.is_melody(e, context)}
    assert all(e.instrument not in others for e in melody), (
        f"旋律 {[e.instrument for e in melody][:3]} が伴奏 {others} と同じ音色"
    )


def test_melody_is_placed_closer_than_the_accompaniment():
    """主旋律を伴奏より近くに置くこと。

    音符ブロックには音量そのものが無い。layout が音量を距離に変換するので、
    強弱をつける手段はこれしかない。採譜のままだと旋律のほうが音量が小さく、
    **旋律が伴奏より遠くに置かれて小さくなる**。
    """
    from mcnb import layout as L

    context = _melody_context()
    song = _mixed_song()

    def distances(config):
        out, _ = A.arrange(song, config)
        melody = [L.target_distance(e.velocity) for e in out.events if A.is_melody(e, context)]
        rest = [L.target_distance(e.velocity) for e in out.events if not A.is_melody(e, context)]
        return sum(melody) / len(melody), sum(rest) / len(rest)

    plain_melody, plain_rest = distances(
        A.ArrangeConfig(context=context, voice_roles=False, emphasize_melody=False)
    )
    assert plain_melody > plain_rest, "前提が崩れている — 素の時点で旋律が近い"

    melody, rest = distances(A.ArrangeConfig(context=context))
    assert melody < rest, f"旋律 {melody:.1f} / 伴奏 {rest:.1f} ブロック"


def test_percussion_keeps_its_voice_and_level():
    """打楽器は音程を持たないので、音色も強弱も触らないこと。

    位置（tick）は格子に載るので動く。ドラムこそ拍に乗るべきなので、それでよい。
    """
    context = _melody_context()
    drums = [
        NoteEvent(tick=t, instrument="snare", midi=64, velocity=0.7) for t in range(0, 32, 4)
    ]
    song = Song(name="d", events=drums)
    out, _ = A.arrange(song, A.ArrangeConfig(context=context, max_concurrent=0))
    assert {(e.instrument, e.velocity) for e in out.events} == {("snare", 0.7)}


def test_quantize_collapses_a_semitone_flutter():
    """1 音のはずが半音を行き来する連打になっているものを、1 音にまとめること。

    採譜は長い音を刻みながら音程を半音揺らすことがある（実測で MIDI 76 の 1 音が
    76/77 を 0.05 秒ごとに行き来する 8 連打になっていた）。音程が違うので
    「同じ音の連打」としては引っかからず、素通りしていた。
    """
    context = _melody_context()
    flutter = [
        _note(tick, 76 if tick % 2 == 0 else 77, 0.6) for tick in range(0, 12)
    ]
    song = Song(name="f", events=flutter)

    plain, _ = A.arrange(song, A.ArrangeConfig(context=context, quantize=False, max_concurrent=0))
    gridded, _ = A.arrange(song, A.ArrangeConfig(context=context, max_concurrent=0))

    def clashes(s):
        by = {}
        for e in s.events:
            by.setdefault(e.tick, []).append(e.midi)
        return sum(1 for v in by.values() for i, a in enumerate(v) for b in v[i + 1:] if abs(a - b) == 1)

    assert clashes(gridded) == 0, "半音のぶつかりが残っている"
    assert len(gridded.events) < len(flutter), (
        f"連打がまとまっていない {len(flutter)} → {len(gridded.events)}"
    )
    # 入力は全部が互いに半音以内なので、1 枠に 1 音しか残らないはず
    per_tick = collections.Counter(e.tick for e in gridded.events)
    assert max(per_tick.values()) == 1, f"同じ枠に複数残っている {per_tick.most_common(3)}"


def test_quantize_keeps_chords():
    """和音は音程が離れているので、まとめずに残すこと。"""
    context = _melody_context()
    chord = [_note(0, m, 0.6) for m in (60, 64, 67, 72)]
    song = Song(name="c", events=chord)
    out, _ = A.arrange(song, A.ArrangeConfig(context=context, max_concurrent=0))
    assert sorted(e.midi for e in out.events) == [60, 64, 67, 72]


def test_the_two_bands_neither_overlap_nor_saturate():
    """主旋律と伴奏の帯が、重ならず、距離の上下限にも当たらないこと。

    重なれば強弱が逆転しうるし、上下限に当たれば帯の中の音が全部同じ距離に
    潰れて抑揚が消える。どちらも定数をいじった拍子に起こる。
    """
    from mcnb import layout as L

    m_lo, m_hi = A.MELODY_BAND
    a_lo, a_hi = A.ACCOMPANIMENT_BAND
    assert a_hi < m_lo, f"帯が重なっている 伴奏{a_hi} / 旋律{m_lo}"

    for name, (lo, hi) in (("主旋律", A.MELODY_BAND), ("伴奏", A.ACCOMPANIMENT_BAND)):
        near, far = L.target_distance(hi), L.target_distance(lo)
        assert near > L.MIN_DISTANCE, f"{name}の近い端が下限に張り付く（{near}）"
        assert far < L.MAX_DISTANCE, f"{name}の遠い端が上限に張り付く（{far}）"
        assert far > near, f"{name}の帯が潰れている"

    assert L.target_distance(m_lo) < L.target_distance(a_hi), "旋律が伴奏より遠い"
