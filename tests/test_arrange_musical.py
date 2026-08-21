"""編曲が「音楽的な役割」で音を選べているかの検証。

これまで、残す音は**音量だけ**で決めていた。大きい音ほど曲の骨格だという
当て推量で、静かな主旋律より賑やかな伴奏を残してしまう。

原音の解析（拍・調・コード・主旋律）が入ったので役割で決められるようになった。
ここで見張るのは「減らしても主旋律が残るか」。音数を合わせても旋律が消えたら、
曲として成り立たない。
"""

from __future__ import annotations

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
