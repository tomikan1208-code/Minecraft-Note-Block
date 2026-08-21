"""1画面から全部やる GUI。

    uv run mcnb gui

ブラウザで開く理由は、**音がその場で鳴らせる**から。私（Claude）は音を聴けないので、
あなたが原曲と MC 版をすぐ聴き比べられる場所が要る。

中身は CLI を叩いて、その出力をそのまま流すだけ。GUI 専用のロジックは持たない
（同じことを2箇所に書かないため）。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8770
#: ログを溜めておく上限（行）
LOG_LIMIT = 4000


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# ジョブ（CLI を1つ動かす）
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    id: str
    label: str
    args: list[str]
    proc: subprocess.Popen | None = None
    lines: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float | None = None
    returncode: int | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "args": self.args,
            "running": self.running,
            "returncode": self.returncode,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "lines": len(self.lines),
        }


class JobRunner:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.current: str | None = None

    def start(self, label: str, args: list[str]) -> Job:
        if self.current and self.jobs[self.current].running:
            raise RuntimeError("別の処理が実行中です。終わるまで待つか停止してください。")

        job = Job(id=uuid.uuid4().hex[:12], label=label, args=args)
        self.jobs[job.id] = job
        self.current = job.id

        job.proc = subprocess.Popen(
            [sys.executable, "-m", "mcnb", *args],
            cwd=repo_root(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**_env()},
        )

        import threading

        def pump() -> None:
            assert job.proc and job.proc.stdout
            for line in job.proc.stdout:
                job.lines.append(line.rstrip("\n"))
                del job.lines[:-LOG_LIMIT]
            job.proc.wait()
            job.returncode = job.proc.returncode
            job.finished = time.time()

        threading.Thread(target=pump, daemon=True).start()
        return job

    def stop(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job and job.proc and job.running:
            job.proc.terminate()
            return True
        return False


def _env() -> dict:
    import os

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


# --------------------------------------------------------------------------- #
# 状態（ページに出す材料）
# --------------------------------------------------------------------------- #


def collect_state() -> dict:
    root = repo_root()
    tests = sorted(p.stem for p in (root / "tests" / "inputs").glob("*.mid"))
    outputs = []
    for pack in sorted((root / "out").glob("*/*_datapack")):
        song_dir = pack.parent
        nbs = next(iter(song_dir.glob("*.nbs")), None)
        outputs.append(
            {
                "name": song_dir.name,
                "datapack": str(pack.relative_to(root)),
                "nbs": str(nbs.relative_to(root)) if nbs else None,
                "functions": len(list((pack / "data" / "mcnb" / "function").rglob("*.mcfunction"))),
            }
        )
    worlds = sorted(p.name for p in (root / ".minecraft" / "saves").glob("*") if p.is_dir())
    cached = []
    for meta in sorted((root / "cache" / "audio").glob("*.json")):
        try:
            cached.append(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    measures = sorted(str(p.relative_to(root)) for p in (root / "out" / "measure").glob("*.png"))
    return {
        "tests": tests,
        "analyses": collect_analyses(root),
        "outputs": outputs[-24:],
        "worlds": worlds,
        "cached": cached[-12:],
        "measure_images": measures,
        "assets_ready": (root / "assets" / "mc" / "manifest.json").is_file(),
    }


#: 確認用 wav の並び順。聴く順番でもある（拍が合っていなければ他は見るまでもない）
ANALYSIS_TRACKS = ("beats", "chords", "melody", "melody_solo")


def collect_analyses(root: Path) -> list[dict]:
    """`mcnb analyze` の結果を拾う。ブラウザでそのまま聴けるようにするため。"""
    found = []
    for folder in sorted((root / "out" / "analysis").glob("*")):
        tracks = [
            {"kind": kind, "path": str((folder / f"{kind}.wav").relative_to(root))}
            for kind in ANALYSIS_TRACKS
            if (folder / f"{kind}.wav").is_file()
        ]
        if not tracks:
            continue
        found.append({"name": folder.name, "summary": _analysis_summary(folder), "tracks": tracks})
    return found[-8:]


def _analysis_summary(folder: Path) -> str:
    """解析 JSON から一行の要約を作る。"""
    try:
        data = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError):
        return ""
    key = (data.get("key") or {}).get("name") or "調不明"
    names = [c["name"] for c in data.get("chords", [])]
    uniq = [n for i, n in enumerate(names) if i == 0 or n != names[i - 1]]
    head = " → ".join(uniq[:8]) + (" …" if len(uniq) > 8 else "")
    return f"{data.get('tempo', 0):.0f} BPM / {key} / {head}"


# --------------------------------------------------------------------------- #
# アプリ
# --------------------------------------------------------------------------- #


def create_app():
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("GUI の依存が入っていません: uv sync --extra gui") from e

    app = FastAPI(title="mcnb")
    runner = JobRunner()
    page = Path(__file__).with_name("gui.html")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page.read_text(encoding="utf-8")

    @app.get("/api/state")
    def state() -> dict:
        current = runner.jobs.get(runner.current or "")
        return {**collect_state(), "current": current.to_dict() if current else None}

    @app.post("/api/run")
    async def run(body: dict = Body(...)) -> dict:
        label = str(body.get("label") or "実行")
        args = [str(a) for a in body.get("args") or []]
        if not args:
            raise HTTPException(400, "args が空です")
        try:
            job = runner.start(label, args)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return job.to_dict()

    @app.post("/api/stop")
    async def stop(body: dict = Body(default={})) -> dict:
        return {"stopped": runner.stop(str(body.get("id") or runner.current or ""))}

    @app.get("/api/log/{job_id}")
    async def log(job_id: str):
        job = runner.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "そのジョブはありません")

        async def stream():
            sent = 0
            while True:
                if sent < len(job.lines):
                    chunk = job.lines[sent:]
                    sent = len(job.lines)
                    for line in chunk:
                        yield f"data: {json.dumps({'line': line})}\n\n"
                if not job.running and sent >= len(job.lines):
                    yield f"data: {json.dumps({'done': True, 'returncode': job.returncode})}\n\n"
                    return
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/file")
    def file(path: str):
        root = repo_root().resolve()
        target = (root / path).resolve()
        if root not in target.parents and target != root:
            raise HTTPException(403, "リポジトリの外は開けません")
        if not target.is_file():
            raise HTTPException(404, str(path))
        return FileResponse(target)

    return app


def serve(host: str = HOST, port: int = PORT, open_browser: bool = True,
          restart: bool = True) -> None:
    import uvicorn

    if restart:
        # 前回の GUI が残っているとポートが埋まって起動できない。
        # 検証・測定で立てた Minecraft サーバも一緒に片付ける
        from . import procs

        procs.cleanup(ports=[port, procs.SERVER_PORT, procs.RCON_PORT])

    app = create_app()
    url = f"http://{host}:{port}/"
    print(f"■ GUI: {url}")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="mcnb の GUI")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--keep-stale", action="store_true",
                    help="前回の残骸を止めずに起動する")
    args = ap.parse_args(argv)

    serve(args.host, args.port, open_browser=not args.no_browser,
          restart=not args.keep_stale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
