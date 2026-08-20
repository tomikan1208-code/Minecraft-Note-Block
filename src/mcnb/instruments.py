"""音符ブロックの楽器テーブル（Minecraft 26.2 基準）。

音符ブロックの楽器は**直下のブロック**で決まる。音程は音符ブロック自身の
``note`` ブロックステート (0-24) で、各楽器は2オクターブしか出ない。
楽器を切り替えることで全体で F♯1–F♯7 の6オクターブに届く。

実測の根拠は docs/02_measurements.md を参照。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 音符ブロックの note ステートの段数（0-24 = 25段 = 2オクターブ）
KEYS_PER_INSTRUMENT = 25

#: note=0 が F♯ になる。MIDI の F♯1 は 30。
#: 各楽器の最低音（note=0 のときの MIDI ノート番号）を base として持つ。


@dataclass(frozen=True)
class Instrument:
    """1つの音符ブロック楽器。"""

    name: str
    #: 直下に置くブロック（名前空間なしの ID）
    block: str
    #: note=0 に対応する MIDI ノート番号
    base_midi: int
    #: 有音程か（打楽器は False）
    pitched: bool
    #: -40 dB まで減衰する時間（秒）。実測値。サステインの再発音間隔に使う
    decay: float

    @property
    def lo_midi(self) -> int:
        return self.base_midi

    @property
    def hi_midi(self) -> int:
        return self.base_midi + KEYS_PER_INSTRUMENT - 1

    def covers(self, midi: int) -> bool:
        return self.pitched and self.lo_midi <= midi <= self.hi_midi

    def key_for(self, midi: int) -> int:
        """MIDI ノート番号 → note ステート (0-24)。範囲外なら ValueError。"""
        key = midi - self.base_midi
        if not 0 <= key < KEYS_PER_INSTRUMENT:
            raise ValueError(f"{self.name} は MIDI {midi} を出せません（{self.lo_midi}-{self.hi_midi}）")
        return key

    #: 減衰時間を game tick で
    @property
    def decay_ticks(self) -> float:
        return self.decay * 20


# F♯1 = MIDI 30, F♯2 = 42, F♯3 = 54, F♯4 = 66, F♯5 = 78
_FS1, _FS2, _FS3, _FS4, _FS5 = 30, 42, 54, 66, 78

#: 26.2 の全20楽器。decay は docs/02_measurements.md の実測（-40 dB）。
INSTRUMENTS: dict[str, Instrument] = {
    i.name: i
    for i in [
        # --- 低音域 F♯1-F♯3 ---
        Instrument("bass", "oak_planks", _FS1, True, 0.313),
        Instrument("didgeridoo", "pumpkin", _FS1, True, 0.274),
        # --- F♯2-F♯4 ---
        Instrument("guitar", "white_wool", _FS2, True, 0.448),
        # --- 中音域 F♯3-F♯5 ---
        Instrument("harp", "dirt", _FS3, True, 0.454),
        Instrument("iron_xylophone", "iron_block", _FS3, True, 0.287),
        Instrument("bit", "emerald_block", _FS3, True, 0.225),
        Instrument("banjo", "hay_block", _FS3, True, 0.216),
        Instrument("pling", "glowstone", _FS3, True, 0.468),
        # 26.1 追加。銅の酸化段階ごとに別の音色
        Instrument("trumpet", "copper_block", _FS3, True, 0.160),
        Instrument("trumpet_exposed", "exposed_copper", _FS3, True, 0.208),
        Instrument("trumpet_weathered", "weathered_copper", _FS3, True, 0.166),
        Instrument("trumpet_oxidized", "oxidized_copper", _FS3, True, 0.212),
        # --- F♯4-F♯6 ---
        Instrument("flute", "clay", _FS4, True, 0.302),
        Instrument("cow_bell", "soul_sand", _FS4, True, 0.095),
        # --- 高音域 F♯5-F♯7 ---
        Instrument("bell", "gold_block", _FS5, True, 0.375),
        Instrument("chime", "packed_ice", _FS5, True, 0.793),
        Instrument("xylophone", "bone_block", _FS5, True, 0.104),
        # --- 無音程 ---
        # sand/gravel は落下するので heavy_core を使う（要 v1 で実機確認）
        Instrument("snare", "heavy_core", _FS3, False, 0.055),
        Instrument("hat", "glass", _FS3, False, 0.026),
        Instrument("basedrum", "stone", _FS3, False, 0.094),
    ]
}

#: NBS の楽器 ID (0-15) → 楽器名
NBS_INSTRUMENT_ORDER = [
    "harp",
    "bass",
    "basedrum",
    "snare",
    "hat",
    "guitar",
    "flute",
    "bell",
    "chime",
    "xylophone",
    "iron_xylophone",
    "cow_bell",
    "didgeridoo",
    "bit",
    "banjo",
    "pling",
]

PITCHED = [i for i in INSTRUMENTS.values() if i.pitched]
PERCUSSION = [i for i in INSTRUMENTS.values() if not i.pitched]

#: 全体の音域
MIN_MIDI = min(i.lo_midi for i in PITCHED)
MAX_MIDI = max(i.hi_midi for i in PITCHED)


def instruments_covering(midi: int) -> list[Instrument]:
    """その音高を出せる楽器を全部返す。音色選択の候補になる。"""
    return [i for i in PITCHED if i.covers(midi)]


def nbs_instrument_name(nbs_id: int) -> str:
    """NBS の楽器 ID を楽器名へ。カスタム楽器は harp に落とす。"""
    if 0 <= nbs_id < len(NBS_INSTRUMENT_ORDER):
        return NBS_INSTRUMENT_ORDER[nbs_id]
    return "harp"


def block_state(instrument: str, key: int) -> str:
    """音符ブロックのブロックステート文字列。

    ``instrument`` ステートは通常は直下のブロックから再計算されるが、
    setblock 時に明示しておくと最初の1回の挙動が安定する。
    """
    if instrument not in INSTRUMENTS:
        raise KeyError(f"未知の楽器: {instrument}")
    # trumpet の酸化違いはブロックステート上はすべて "trumpet"
    state_name = "trumpet" if instrument.startswith("trumpet") else instrument
    return f"minecraft:note_block[note={key},instrument={state_name},powered=false]"
