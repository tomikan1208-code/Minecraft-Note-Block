"""URL から音源を取ってくる（YouTube など、yt-dlp が対応するもの）。

    uv run mcnb build "https://www.youtube.com/watch?v=..." --world mysong

ダウンロードしたものは ``cache/audio/`` に置いて使い回す。同じ URL を二度落とさない。

**権利について**: 取得した音源の扱いは利用者の責任。個人的に音符ブロックへ
編曲して自分のワールドで鳴らすぶんには問題になりにくいが、
公開・配布する場合は元の権利者の条件を確認すること。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: URL とみなすパターン
URL_RE = re.compile(r"^https?://", re.I)
#: 落とす音声フォーマット。m4a は YouTube で安定して取れる
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio/best"

DEFAULT_CACHE = Path("cache/audio")


class FetchError(RuntimeError):
    pass


#: flat.io の楽譜置き場
SCORE_CACHE = Path("cache/scores")
#: flat.io の URL から楽譜 ID を取り出す
FLAT_SCORE_RE = re.compile(r"flat\.io/score/([0-9a-f]{24})")


def is_flat_url(text: str) -> bool:
    """flat.io の楽譜ページか。"""
    return bool(FLAT_SCORE_RE.search(text or ""))


def fetch_flat_score(url: str, cache_dir: Path | None = None) -> tuple[Path, str]:
    """flat.io の楽譜を落として ``(パス, 曲名)`` を返す。

    公開されている楽譜なら、埋め込みプレイヤーが使うのと同じ経路で取れる。
    ただし **Referer と Origin が要る**（付けないと 402 が返る）。
    書き出し API（mxl / midi）は有料プランだが、プレイヤー用の JSON は公開。
    """
    import urllib.request

    match = FLAT_SCORE_RE.search(url)
    if not match:
        raise FetchError(f"flat.io の楽譜 URL ではありません: {url}")
    score_id = match.group(1)

    cache = Path(cache_dir or SCORE_CACHE)
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"flat_{score_id}.json"

    headers = {
        "Referer": f"https://flat.io/score/{score_id}",
        "Origin": "https://flat.io",
        "User-Agent": "Mozilla/5.0",
    }

    def get(path: str) -> bytes:
        request = urllib.request.Request(
            f"https://api.flat.io/v2/{path}", headers=headers
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()

    title = score_id
    try:
        meta = json.loads(get(f"scores/{score_id}"))
        title = str(meta.get("title") or score_id)
    except Exception:  # noqa: BLE001 — 題名が取れなくても本体があれば進む
        pass

    if not dest.is_file():
        try:
            dest.write_bytes(get(f"scores/{score_id}/revisions/last/json"))
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"楽譜を取得できません（非公開かもしれません）: {e}") from e
    return dest, title


def is_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


@dataclass(frozen=True)
class Media:
    path: Path
    title: str
    uploader: str
    duration: float
    url: str
    cached: bool

    @property
    def slug(self) -> str:
        """ファイル名やワールド名に使える形。"""
        base = re.sub(r"[^\w\-]+", "_", self.title, flags=re.UNICODE).strip("_")
        return base[:60] or "audio"


def _ydl():
    try:
        import yt_dlp
    except ImportError as e:
        raise FetchError("yt-dlp が入っていません: uv sync --extra audio") from e
    return yt_dlp


def probe(url: str) -> dict:
    """ダウンロードせずにメタデータだけ取る。"""
    yt_dlp = _ydl()
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "noplaylist": True}) as y:
        return y.extract_info(url, download=False)


#: imageio-ffmpeg の同梱バイナリを ffmpeg.exe として置く場所
FFMPEG_BIN = Path("cache/bin")


def _ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 — 無ければ無いで進める
        return None


def ensure_ffmpeg_on_path(bin_dir: Path | None = None) -> Path | None:
    """``ffmpeg`` を PATH から引ける状態にして、そのディレクトリを返す。

    audio-separator は ``shutil.which("ffmpeg")`` で存在確認するので、
    imageio-ffmpeg の同梱バイナリ（名前がバージョン入りで which に引っかからない）を
    ``ffmpeg.exe`` としてコピーしておく。

    **この処理のあとの ``os.environ["PATH"]`` にそのディレクトリが入る。**
    子プロセスに渡すだけなら呼び出し側で env を組んでもよいが、
    audio-separator を同じプロセスの中で動かす場合はこれが要る。
    """
    if shutil.which("ffmpeg"):
        return Path(shutil.which("ffmpeg")).parent

    exe = _ffmpeg()
    if exe is None:
        return None

    bin_dir = Path(bin_dir or FFMPEG_BIN)
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not target.is_file():
        shutil.copy2(exe, target)

    resolved = bin_dir.resolve()
    current = os.environ.get("PATH", "")
    if str(resolved) not in current.split(os.pathsep):
        os.environ["PATH"] = f"{resolved}{os.pathsep}{current}"
    return resolved


def fetch(url: str, cache_dir: Path | None = None, to_wav: bool = True) -> Media:
    """URL から音源を落として ``Media`` を返す。既に落としてあれば再利用する。"""
    yt_dlp = _ydl()
    cache = Path(cache_dir or DEFAULT_CACHE)
    cache.mkdir(parents=True, exist_ok=True)

    info = probe(url)
    video_id = info.get("id") or re.sub(r"\W+", "_", url)[-24:]
    title = info.get("title") or video_id
    meta_path = cache / f"{video_id}.json"
    existing = sorted(cache.glob(f"{video_id}.*"))
    audio = next((p for p in existing if p.suffix.lower() not in (".json",)), None)

    if audio is None:
        opts = {
            "format": AUDIO_FORMAT,
            "outtmpl": str(cache / f"{video_id}.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [_progress],
        }
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
        audio = next(
            (p for p in sorted(cache.glob(f"{video_id}.*")) if p.suffix.lower() != ".json"), None
        )
        if audio is None:
            raise FetchError(f"ダウンロードに失敗しました: {url}")
        cached = False
    else:
        cached = True

    meta_path.write_text(
        json.dumps(
            {
                "id": video_id,
                "title": title,
                "uploader": info.get("uploader"),
                "duration": info.get("duration"),
                "webpage_url": info.get("webpage_url") or url,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 解析側（librosa / audio-separator）が確実に読める形に揃えておく
    if to_wav and audio.suffix.lower() != ".wav":
        wav = audio.with_suffix(".wav")
        if not wav.is_file():
            ffmpeg = _ffmpeg()
            if ffmpeg is None:
                raise FetchError("ffmpeg が見つかりません: uv sync --extra audio")
            subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", str(audio),
                 "-ac", "2", "-ar", "44100", str(wav)],
                check=True,
            )
        audio = wav

    return Media(
        path=audio,
        title=title,
        uploader=info.get("uploader") or "",
        duration=float(info.get("duration") or 0),
        url=info.get("webpage_url") or url,
        cached=cached,
    )


def _progress(status: dict) -> None:
    if status.get("status") != "downloading":
        return
    total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
    done = status.get("downloaded_bytes") or 0
    if total:
        print(f"\r  取得中 {done * 100 / total:5.1f}%  ({total / 1048576:.1f} MB)", end="", flush=True)


def trim(src: Path, seconds: float, dest: Path | None = None) -> Path:
    """先頭 ``seconds`` 秒だけ切り出す。パイプラインの試運転用。"""
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        raise FetchError("ffmpeg が見つかりません: uv sync --extra audio")
    dest = dest or src.with_name(f"{src.stem}_first{int(seconds)}s.wav")
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-t", str(seconds), "-i", str(src),
         "-ac", "2", "-ar", "44100", str(dest)],
        check=True,
    )
    return dest


def main(argv: list[str] | None = None) -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="URL から音源を取得する")
    ap.add_argument("url")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--info", action="store_true", help="メタデータだけ表示して落とさない")
    ap.add_argument("--trim", type=float, default=None, metavar="SEC", help="先頭だけ切り出す")
    args = ap.parse_args(argv)

    try:
        if args.info:
            info = probe(args.url)
            d = int(info.get("duration") or 0)
            print(f"タイトル : {info.get('title')}")
            print(f"チャンネル: {info.get('uploader')}")
            print(f"長さ     : {d // 60}:{d % 60:02d}")
            return 0

        media = fetch(args.url, args.cache)
        print()
        print(f"  {media.title}")
        print(f"  {media.uploader} / {int(media.duration) // 60}:{int(media.duration) % 60:02d}")
        print(f"  {media.path}" + ("  (キャッシュ)" if media.cached else ""))
        if args.trim:
            clipped = trim(media.path, args.trim)
            print(f"  切り出し: {clipped}")
    except FetchError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
