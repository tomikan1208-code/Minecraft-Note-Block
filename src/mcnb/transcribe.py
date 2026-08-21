"""音源を「音符」に起こす — 譜面に相当する中間表現を作る。

これまでの採譜（hyperchoron の音声経路）は CQT のピークを拾うので、
**音符ではなくスペクトルの写し**が出る。毎秒 190 音になり、
1 音ずつに意味が無い。

ここでは Basic Pitch（Spotify、Apache-2.0）で**音符**を出す。
開始・終了・音高を持つので、そのまま譜面に書き起こせる。

譜面つきの曲で答え合わせした結果（親愛なるあなたは火葬・アウトロ）::

    正解の譜面      191 音 / MIDI 46〜76
    Basic Pitch    217 音 / MIDI 46〜76（音域は完全一致）
    譜面の音の 92% を再現

Basic Pitch は tensorflow を要求し、tensorflow は Python 3.13 用のホイールを
出していない。**別プロセスの Python 3.11 で動かす**ことで本体環境は 3.13 のまま
にする。採譜は独立した工程なので、これで困らない。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .song import TICKS_PER_SECOND, Song

#: 採譜結果の置き場
CACHE_DIR = Path("cache/transcribe")
#: Basic Pitch を動かす Python の版
RUNNER_PYTHON = "3.11"


class TranscribeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Note:
    """音符 1 つ。譜面に書けるだけの情報を持つ。"""

    start: float        # 秒
    end: float
    midi: int
    velocity: float     # 0..1

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _find_python(version: str = RUNNER_PYTHON) -> str:
    """Basic Pitch を動かす Python を探す。

    uv の「マイナー版へのリンク」は壊れることがあるので、
    実体のディレクトリを直接探す。
    """
    roots = [
        Path.home() / "AppData" / "Roaming" / "uv" / "python",   # Windows
        Path.home() / ".local" / "share" / "uv" / "python",       # Linux / macOS
    ]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.glob(f"cpython-{version}.*"):
            for name in ("python.exe", "bin/python3", "bin/python"):
                exe = entry / name
                if exe.is_file():
                    found.append(exe)
                    break
    if found:
        return str(sorted(found)[-1])

    # uv に用意させる
    try:
        subprocess.run(["uv", "python", "install", version], check=True,
                       capture_output=True, timeout=900)
    except (OSError, subprocess.SubprocessError) as e:
        raise TranscribeError(f"Python {version} を用意できません: {e}") from e
    for root in roots:
        for entry in root.glob(f"cpython-{version}.*"):
            for name in ("python.exe", "bin/python3", "bin/python"):
                exe = entry / name
                if exe.is_file():
                    return str(exe)
    raise TranscribeError(f"Python {version} が見つかりません")


#: 別プロセスで動かす中身。MIDI ではなく JSON で受け取る
#: （MIDI を経由するとテンポや分解能の解釈でずれが入る）
_RUNNER = '''
import json, sys
from basic_pitch.inference import predict
from basic_pitch import ICASSP_2022_MODEL_PATH

audio, out = sys.argv[1], sys.argv[2]
_, _, notes = predict(audio, ICASSP_2022_MODEL_PATH)
rows = [
    {"start": float(s), "end": float(e), "midi": int(p), "velocity": float(v)}
    for s, e, p, v, *_ in notes
]
with open(out, "w", encoding="utf-8") as f:
    json.dump(rows, f)
print(f"notes={len(rows)}")
'''


def transcribe(
    audio: Path | str,
    cache_dir: Path | None = None,
    force: bool = False,
    verbose: bool = True,
) -> list[Note]:
    """音源を音符に起こす。前に起こしてあればそれを読む。"""
    audio = Path(audio)
    if not audio.is_file():
        raise TranscribeError(f"音源が見つかりません: {audio}")

    cache_dir = Path(cache_dir or CACHE_DIR)
    cached = cache_dir / f"{audio.stem}.json"
    if cached.is_file() and not force:
        try:
            rows = json.loads(cached.read_text(encoding="utf-8"))
            if verbose:
                print(f"  採譜済みを使います: {cached}")
            return [Note(**r) for r in rows]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    python = _find_python()
    cache_dir.mkdir(parents=True, exist_ok=True)
    runner = cache_dir / "_run_basic_pitch.py"
    runner.write_text(_RUNNER, encoding="utf-8")

    if verbose:
        print("  音符に起こしています（Basic Pitch）…")
        print("  初回はモデルと依存を落とすので時間がかかります")

    if not shutil.which("uv"):
        raise TranscribeError("uv が見つかりません")

    # --no-project が要る。付けないと本体の .venv を 3.11 で作り直そうとして壊す
    command = [
        "uv", "run", "--no-project", "--python", python,
        "--with", "basic-pitch", "--with", "setuptools<81",
        "python", str(runner), str(audio), str(cached),
    ]
    proc = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**_env(), "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode != 0 or not cached.is_file():
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-6:])
        raise TranscribeError(f"Basic Pitch が失敗しました:\n{tail}")

    rows = json.loads(cached.read_text(encoding="utf-8"))
    return [Note(**r) for r in rows]


# --------------------------------------------------------------------------- #
# ピアノ専用（ByteDance の高分解能ピアノ採譜）
# --------------------------------------------------------------------------- #

#: 重みの置き場と取得元。ライブラリは wget を呼ぶので Windows では動かない
PIANO_MODEL = Path("models/piano/note_F1=0.9677_pedal_F1=0.9186.pth")
PIANO_MODEL_URL = (
    "https://zenodo.org/record/4034264/files/"
    "CRNN_note_F1%3D0.9677_pedal_F1%3D0.9186.pth?download=1"
)
#: これより小さいファイルは途中で切れているとみなす
PIANO_MODEL_MIN_BYTES = 160_000_000


def ensure_piano_model(path: Path | None = None, verbose: bool = True) -> Path:
    """ピアノ採譜の重みを用意する。"""
    import urllib.request

    path = Path(path or PIANO_MODEL)
    if path.is_file() and path.stat().st_size >= PIANO_MODEL_MIN_BYTES:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"  ピアノ採譜の重みを取得します（約 164MB）: {path}")
    try:
        urllib.request.urlretrieve(PIANO_MODEL_URL, path)
    except OSError as e:
        raise TranscribeError(f"重みを取得できません: {e}") from e
    if path.stat().st_size < PIANO_MODEL_MIN_BYTES:
        path.unlink(missing_ok=True)
        raise TranscribeError("重みの取得が途中で切れました")
    return path


def transcribe_piano(
    audio: Path | str,
    cache_dir: Path | None = None,
    force: bool = False,
    verbose: bool = True,
) -> list[Note]:
    """ピアノ曲を音符に起こす（ByteDance の高分解能ピアノ採譜）。

    ピアノ専用だが、そのぶん正確。譜面つきの曲での実測::

        正解の譜面           191 音 / MIDI 46〜76
        Basic Pitch         217 音 / 46〜76  譜面の再現 93%
        ピアノ専用            223 音 / 46〜87  譜面の再現 98%

    再現は上だが、譜面に無い高い音も出す（音域が 87 まで伸びている）。
    Basic Pitch は音域が譜面と完全に一致するぶん素直。曲によって選ぶ。

    Python 3.13 でそのまま動くので、Basic Pitch と違って別プロセスが要らない。
    """
    audio = Path(audio)
    if not audio.is_file():
        raise TranscribeError(f"音源が見つかりません: {audio}")

    cache_dir = Path(cache_dir or CACHE_DIR)
    cached = cache_dir / f"{audio.stem}_piano.json"
    if cached.is_file() and not force:
        try:
            rows = json.loads(cached.read_text(encoding="utf-8"))
            if verbose:
                print(f"  採譜済みを使います: {cached}")
            return [Note(**r) for r in rows]
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    try:
        import librosa
        import torch
        from piano_transcription_inference import PianoTranscription, sample_rate
    except ImportError as e:
        raise TranscribeError(
            "ピアノ採譜の依存が入っていません: uv sync --extra transcribe"
        ) from e

    model = ensure_piano_model(verbose=verbose)
    if verbose:
        print("  音符に起こしています（ピアノ専用）…")

    y, _ = librosa.load(str(audio), sr=sample_rate, mono=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    engine = PianoTranscription(device=device, checkpoint_path=str(model))
    result = engine.transcribe(y, None)

    notes = [
        Note(start=float(n["onset_time"]), end=float(n["offset_time"]),
             midi=int(n["midi_note"]), velocity=float(n.get("velocity", 100)) / 127.0)
        for n in result["est_note_events"]
    ]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(
        json.dumps([n.__dict__ for n in notes], ensure_ascii=False), encoding="utf-8"
    )
    return notes


def _env() -> dict:
    import os

    return dict(os.environ)


def summarize(notes: list[Note]) -> str:
    if not notes:
        return "  音符なし"
    length = max(n.end for n in notes)
    mids = [n.midi for n in notes]
    durations = sorted(n.duration for n in notes)
    return "\n".join([
        f"  音符      : {len(notes)} 個 / {length:.1f} 秒 ({len(notes)/max(length,1e-9):.1f} 音/秒)",
        f"  音域      : MIDI {min(mids)}〜{max(mids)}",
        f"  長さ      : 中央 {durations[len(durations)//2]:.2f} 秒",
    ])


def to_song(
    notes: list[Note],
    name: str = "transcribed",
    blocks_per_note: int = 1,
) -> Song:
    """起こした音符を Song にする。楽譜から置くのと同じ経路を通す。"""
    from .score import layered_events
    
    items = [
        (max(0, round(n.start * TICKS_PER_SECOND)), n.midi,
         max(0.05, min(1.0, n.velocity)), 0.0)
        for n in notes
    ]
    return Song(name=name, events=layered_events(items, blocks_per_note),
                source="basic-pitch")


def main(argv: list[str] | None = None) -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="音源を音符に起こす（Basic Pitch）")
    ap.add_argument("audio")
    ap.add_argument("--force", action="store_true", help="採譜済みでもやり直す")
    args = ap.parse_args(argv)

    try:
        notes = transcribe(args.audio, force=args.force)
    except TranscribeError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    print(summarize(notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
