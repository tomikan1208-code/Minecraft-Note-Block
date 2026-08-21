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


def to_song(score: Score, name: str = "score", tempo: float | None = None) -> Song:
    """譜面を Song にする。**音は足しも引きもしない。**"""
    bpm = tempo or score.tempo
    seconds_per_beat = 60.0 / bpm

    events: list[NoteEvent] = []
    dropped = 0
    for note in score.notes:
        voice = voice_for(note.midi)
        if voice is None:
            dropped += 1
            continue
        at = ((note.measure - 1) * score.beats_per_bar + note.beat) * seconds_per_beat
        events.append(NoteEvent(
            tick=max(0, round(at * TICKS_PER_SECOND)),
            instrument=voice, midi=note.midi, velocity=DEFAULT_VELOCITY,
            # 左手（下の段）を左、右手を右に振る。人が弾く配置に合わせる
            panning=-0.35 if note.staff >= 2 else 0.35,
        ))

    events.sort(key=lambda e: (e.tick, e.midi))
    song = Song(name=name, events=events, source=str(score.title))
    if dropped:
        song.source += f"（音域外で置けなかった音 {dropped}）"
    return song


def load(path: Path | str, name: str | None = None, tempo: float | None = None) -> Song:
    score = read_score(path)
    return to_song(score, name=name or Path(path).stem, tempo=tempo)


def summary_or_empty(score: Score) -> str:
    """表示用。壊れた譜面でも落ちないようにする。"""
    try:
        return score.summary()
    except (ValueError, KeyError):
        return f"  音符 {len(score.notes)} 個"
