"""楽譜（MusicXML）をそのまま音符ブロックにする。

採譜を通さない経路。**譜面に書いてあるとおりに置くだけ**で、
音を足しも引きもしない。

これがあると、音源からの採譜がどれだけ壊しているかが切り分けられる。
譜面から置いたものが良く鳴るなら、問題は採譜にある。

    uv run mcnb build score.musicxml

対応する形は 2 つ:

* 標準の MusicXML（``.xml`` / ``.musicxml`` / ``.mxl``）
* flat.io が返す JSON（``score-partwise`` を持つ同じ構造）
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .instruments import INSTRUMENTS
from .song import TICKS_PER_SECOND, NoteEvent, Song

#: 音名から半音への対応
STEP_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

#: 音域ごとに使う楽器。上から順に、音が入るものを使う。
#: harp（土）が Minecraft でいちばんピアノに近い。
SCORE_VOICES = ("harp", "bass", "guitar", "bell", "flute", "chime")

#: テンポが書かれていないときの既定
DEFAULT_TEMPO = 120.0
#: 譜面の音量（MusicXML の dynamics は任意なので、既定を置く）
DEFAULT_VELOCITY = 0.72


class ScoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScoreNote:
    """譜面の音符 1 つ。"""

    measure: int
    beat: float          # 小節頭からの拍数
    midi: int
    duration: float      # 拍
    staff: int = 1


@dataclass
class Score:
    notes: list[ScoreNote]
    tempo: float
    beats_per_bar: int
    fifths: int
    title: str = ""

    @property
    def key_name(self) -> str:
        names = ["C", "G", "D", "A", "E", "B", "F#", "C#"]
        flats = ["C", "F", "A#", "D#", "G#", "C#", "F#", "B"]
        n = self.fifths
        return names[n] if n >= 0 else flats[-n]

    def summary(self) -> str:
        mids = [n.midi for n in self.notes]
        return "\n".join([
            f"  曲名      : {self.title or '（無題）'}",
            f"  テンポ    : {self.tempo:g} BPM  {self.beats_per_bar}/4",
            f"  調号      : {self.fifths:+d} → {self.key_name}",
            f"  音符      : {len(self.notes)} 個 / {max(n.measure for n in self.notes)} 小節",
            f"  音域      : MIDI {min(mids)}〜{max(mids)}",
        ])


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _pitch_to_midi(pitch: dict) -> int:
    return (
        12 * (int(pitch["octave"]) + 1)
        + STEP_SEMITONES[pitch["step"]]
        + int(pitch.get("alter") or 0)
    )


def _xml_to_dict(element) -> dict | str:
    """MusicXML の要素を、flat.io の JSON と同じ形の辞書にする。"""
    children: dict = {}
    for child in element:
        value = _xml_to_dict(child)
        if child.tag in children:
            if not isinstance(children[child.tag], list):
                children[child.tag] = [children[child.tag]]
            children[child.tag].append(value)
        else:
            children[child.tag] = value
    for key, value in element.attrib.items():
        children[f"${key}"] = value
    if not children:
        return (element.text or "").strip()
    if element.text and element.text.strip():
        children["#text"] = element.text.strip()
    return children


def _load_partwise(path: Path) -> dict:
    """ファイルから ``score-partwise`` の辞書を取り出す。"""
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if "score-partwise" not in data:
            raise ScoreError("score-partwise がありません")
        return data["score-partwise"]

    if suffix == ".mxl":
        with zipfile.ZipFile(path) as zf:
            name = next(
                (n for n in zf.namelist() if n.endswith((".xml", ".musicxml")) and "META-INF" not in n),
                None,
            )
            if name is None:
                raise ScoreError(".mxl の中に楽譜が見つかりません")
            root = ElementTree.fromstring(zf.read(name))
    else:
        root = ElementTree.parse(path).getroot()

    if root.tag != "score-partwise":
        raise ScoreError(f"score-partwise ではありません: {root.tag}")
    result = _xml_to_dict(root)
    if not isinstance(result, dict):
        raise ScoreError("楽譜を読めませんでした")
    return result


def read_score(path: Path | str) -> Score:
    """MusicXML を読んで ``Score`` にする。"""
    path = Path(path)
    partwise = _load_partwise(path)

    title = ""
    for credit in _as_list(partwise.get("credit")):
        if isinstance(credit, dict) and credit.get("credit-type") == "title":
            title = str(credit.get("credit-words", ""))
    if not title:
        work = partwise.get("work")
        if isinstance(work, dict):
            title = str(work.get("work-title", "") or "")

    notes: list[ScoreNote] = []
    tempo = None
    beats_per_bar, fifths = 4, 0
    divisions = 1

    for part in _as_list(partwise.get("part")):
        divisions = 1
        for measure in _as_list(part.get("measure")):
            number = int(measure.get("$number", len(notes) and 0 or 1) or 1)

            for attrs in _as_list(measure.get("attributes")):
                if not isinstance(attrs, dict):
                    continue
                if attrs.get("divisions"):
                    divisions = int(attrs["divisions"])
                for time in _as_list(attrs.get("time")):
                    if isinstance(time, dict) and time.get("beats"):
                        beats_per_bar = int(time["beats"])
                for key in _as_list(attrs.get("key")):
                    if isinstance(key, dict) and key.get("fifths") is not None:
                        fifths = int(key["fifths"])

            if tempo is None:
                tempo = _find_tempo(measure)

            cursor = 0.0
            previous = 0.0
            for note in _as_list(measure.get("note")):
                if not isinstance(note, dict):
                    continue
                length = float(note.get("duration", 0) or 0) / divisions
                # 位置は書いてあればそれを使う。無ければ音価を積み上げる
                location = note.get("$adagio-location")
                if isinstance(location, dict) and "timePos" in location:
                    at = float(location["timePos"]) / divisions
                elif "chord" in note:
                    at = cursor - previous
                else:
                    at = cursor

                if "rest" not in note and isinstance(note.get("pitch"), dict):
                    notes.append(ScoreNote(
                        measure=number, beat=at, midi=_pitch_to_midi(note["pitch"]),
                        duration=length, staff=int(note.get("staff", 1) or 1),
                    ))
                if "chord" not in note:
                    previous = length
                    cursor += length

    if not notes:
        raise ScoreError("音符が 1 つもありません")
    return Score(notes=notes, tempo=tempo or DEFAULT_TEMPO,
                 beats_per_bar=beats_per_bar, fifths=fifths, title=title)


def _find_tempo(measure: dict) -> float | None:
    """小節の中からテンポ指定を探す。"""
    for sound in _as_list(measure.get("sound")):
        if isinstance(sound, dict):
            for key in ("$tempo", "tempo"):
                if sound.get(key):
                    return float(sound[key])
    for direction in _as_list(measure.get("direction")):
        if not isinstance(direction, dict):
            continue
        for dtype in _as_list(direction.get("direction-type")):
            if isinstance(dtype, dict) and isinstance(dtype.get("metronome"), dict):
                per = dtype["metronome"].get("per-minute")
                if per:
                    return float(per)
    return None


def voice_for(midi: int) -> str | None:
    """その音高を出せる楽器。ピアノに近い順に見る。"""
    for name in SCORE_VOICES:
        inst = INSTRUMENTS.get(name)
        if inst and inst.base_midi <= midi <= inst.base_midi + 24:
            return name
    return None


def place(midi: int) -> tuple[str, int] | None:
    """置ける楽器と音高。音域外ならオクターブで折り返す。

    音符ブロック全体で出せるのは MIDI 30〜102。ピアノ譜はそれより外まで
    使うので（真っ黒ナイト・オブ・ナイツは 21〜108）、外れたぶんは
    オクターブ単位で中へ寄せる。音名は変わらない。
    """
    for shift in (0, 12, -12, 24, -24, 36, -36):
        moved = midi + shift
        voice = voice_for(moved)
        if voice:
            return voice, moved
    return None


#: 1 つの音符に使う音符ブロックの数ごとの、重ねる音程（半音）。
#: 音符ブロックは 1 個で 1 音しか出せず、しかも音色ごとに 2 オクターブしかない。
#: 「1 音 = 1 ブロック」に縛られると、ピアノ 1 音の厚みが出せない。
#: 2 個目は 1 オクターブ下（土台）、3 個目は 1 オクターブ上（きらめき）。
LAYER_OFFSETS = (0, -12, +12)
#: 重ねる音の音量。元の音より小さくして、主役を食わないようにする
LAYER_GAINS = (1.0, 0.55, 0.40)


def layered_events(
    items: list[tuple[int, int, float, float]], blocks_per_note: int = 1
) -> list[NoteEvent]:
    """``(tick, midi, 音量, 定位)`` の並びを、重ねかたに従って音符ブロックにする。

    楽譜から置くときも、採譜した音符から置くときも、ここを通す。
    **音符は増やさない。** 増えるのは 1 音符に使うブロックの数だけ。
    """
    layers = max(1, min(blocks_per_note, len(LAYER_OFFSETS)))
    events: list[NoteEvent] = []
    seen: set[tuple[int, int]] = set()

    for tick, midi, velocity, panning in items:
        for offset, gain in zip(LAYER_OFFSETS[:layers], LAYER_GAINS[:layers], strict=True):
            spot = place(midi + offset)
            if spot is None:
                continue
            voice, target = spot
            if (tick, target) in seen:
                continue          # 折り返しで既にある高さと重なった
            seen.add((tick, target))
            events.append(NoteEvent(
                tick=tick, instrument=voice, midi=target,
                velocity=max(0.02, min(1.0, velocity * gain)),
                panning=panning if offset else panning * 0.5,
            ))

    events.sort(key=lambda e: (e.tick, e.midi))
    return events


def to_song(
    score: Score,
    name: str = "score",
    tempo: float | None = None,
    blocks_per_note: int = 1,
) -> Song:
    """譜面を Song にする。**譜面に無い音符は増やさない。**

    ``blocks_per_note`` は 1 つの音符に使う音符ブロックの数。
    1 なら書いてあるとおり。2 以上ならオクターブを重ねて厚みを出す
    （音符は増えないが、鳴らすブロックは増える）。
    """
    bpm = tempo or score.tempo
    seconds_per_beat = 60.0 / bpm
    layers = max(1, min(blocks_per_note, len(LAYER_OFFSETS)))

    events: list[NoteEvent] = []
    dropped = 0
    for note in score.notes:
        at = ((note.measure - 1) * score.beats_per_bar + note.beat) * seconds_per_beat
        tick = max(0, round(at * TICKS_PER_SECOND))
        # 左手（下の段）を左、右手を右に振る。人が弾く配置に合わせる
        panning = -0.35 if note.staff >= 2 else 0.35

        placed = 0
        for offset, gain in zip(LAYER_OFFSETS[:layers], LAYER_GAINS[:layers], strict=True):
            spot = place(note.midi + offset)
            if spot is None:
                continue
            voice, midi = spot
            if placed and any(e.tick == tick and e.midi == midi for e in events[-8:]):
                continue                      # 折り返しで元の音と同じ高さになった
            events.append(NoteEvent(
                tick=tick, instrument=voice, midi=midi,
                velocity=DEFAULT_VELOCITY * gain,
                panning=panning if offset else panning * 0.5,
            ))
            placed += 1
        if not placed:
            dropped += 1

    events.sort(key=lambda e: (e.tick, e.midi))
    song = Song(name=name, events=events, source=str(score.title))
    if dropped:
        song.source += f"（置けなかった音 {dropped}）"
    return song


def load(path: Path | str, name: str | None = None, tempo: float | None = None,
         blocks_per_note: int = 1) -> Song:
    score = read_score(path)
    return to_song(score, name=name or Path(path).stem, tempo=tempo,
                   blocks_per_note=blocks_per_note)


def summary_or_empty(score: Score) -> str:
    """表示用。壊れた譜面でも落ちないようにする。"""
    try:
        return score.summary()
    except (ValueError, KeyError):
        return f"  音符 {len(score.notes)} 個"
