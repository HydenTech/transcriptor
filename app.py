#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atelier de cours — backend.

Pipeline : audio -> faster-whisper (GPU) -> claude -p (CLAUDE.md) -> PDF.
Se lance dans WSL, s'ouvre dans le navigateur Windows sur http://localhost:8765
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("ATELIER_JOBS", ROOT / "cours"))
CLAUDE_MD = ROOT / "CLAUDE.md"
PDF_SCRIPT = ROOT / "scripts" / "generate_pdf.py"

JOBS_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".opus", ".mp4", ".aac",
                  ".wma", ".mpeg", ".mpg", ".mpga", ".mp2", ".webm", ".mkv", ".mov",
                  ".avi", ".wmv", ".aiff", ".aif", ".amr", ".3gp", ".m4b", ".m4v", ".ts"}

STEPS = ("transcription", "synthese", "pdf")

# Le modèle est lourd à charger : on le garde en mémoire entre deux cours.
_model_cache: dict[str, Any] = {}
_jobs: dict[str, "Job"] = {}


def hhmm(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


class Job:
    def __init__(self, job_id: str, folder: Path, audio: Path, options: dict[str, Any]):
        self.id = job_id
        self.folder = folder
        self.audio = audio
        self.options = options
        self.created = time.time()
        self.state = "running"
        self.error: Optional[str] = None
        self.usage: dict[str, Any] = {}
        self.steps = {
            name: {"status": "pending", "progress": 0.0, "detail": ""} for name in STEPS
        }
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.loop = asyncio.get_event_loop()

    # -- diffusion vers l'interface -------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state,
            "error": self.error,
            "usage": self.usage,
            "steps": self.steps,
            "name": self.audio.stem,
        }

    def push(self) -> None:
        payload = json.dumps(self.snapshot(), ensure_ascii=False)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, payload)

    def step(self, name: str, *, status: str | None = None,
             progress: float | None = None, detail: str | None = None) -> None:
        s = self.steps[name]
        if status is not None:
            s["status"] = status
        if progress is not None:
            s["progress"] = max(0.0, min(1.0, progress))
        if detail is not None:
            s["detail"] = detail
        self.push()

    def fail(self, name: str, message: str) -> None:
        self.state = "error"
        self.error = message
        self.steps[name]["status"] = "error"
        self.steps[name]["detail"] = message
        self.push()


# ----------------------------------------------------------------------
# Étape 1 — transcription
# ----------------------------------------------------------------------
def build_initial_prompt(matiere: str, termes: str) -> str:
    matiere = matiere.strip() or "cours magistral"
    prompt = f"Cours magistral de {matiere}."
    termes = termes.strip().strip(",")
    if termes:
        prompt += f" Vocabulaire : {termes}."
    # Whisper tronque l'amorce au-delà d'environ 224 tokens.
    return prompt[:900]


def load_model(name: str):
    if name not in _model_cache:
        from faster_whisper import WhisperModel

        device = os.environ.get("ATELIER_DEVICE", "cuda")
        compute = os.environ.get("ATELIER_COMPUTE", "float16")
        try:
            _model_cache[name] = WhisperModel(name, device=device, compute_type=compute)
        except Exception:
            # Repli CPU si CUDA n'est pas disponible dans WSL.
            _model_cache[name] = WhisperModel(name, device="cpu", compute_type="int8")
    return _model_cache[name]


def transcribe(job: Job) -> Path:
    opts = job.options
    job.step("transcription", status="running", detail="Chargement du modèle")

    model = load_model(opts["modele"])
    job.step("transcription", detail="Analyse de l'audio")

    segments, info = model.transcribe(
        str(job.audio),
        language="fr",
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        initial_prompt=build_initial_prompt(opts["matiere"], opts["termes"]),
    )

    total = max(float(info.duration or 0.0), 1.0)
    out = job.folder / "transcript.txt"
    started = time.time()
    last_mark = -1
    buffer: list[str] = []

    with out.open("w", encoding="utf-8") as fh:
        header = [f"Cours : {opts['matiere'] or 'à préciser'}",
                  f"Fichier : {job.audio.name}",
                  f"Durée : {hhmm(total)}", ""]
        fh.write("\n".join(header))

        for seg in segments:
            minute = int(seg.start // 60)
            if minute != last_mark:
                if buffer:
                    fh.write(" ".join(buffer).strip() + "\n\n")
                    buffer = []
                fh.write(f"[{hhmm(seg.start)}] ")
                last_mark = minute
            buffer.append(seg.text.strip())

            done = seg.end / total
            elapsed = time.time() - started
            reste = elapsed / done - elapsed if done > 0.02 else 0
            job.step("transcription", progress=done,
                     detail=f"{hhmm(seg.end)} / {hhmm(total)}"
                            + (f" · reste ~{int(reste // 60)} min" if reste > 60 else ""))

        if buffer:
            fh.write(" ".join(buffer).strip() + "\n")

    words = len(out.read_text(encoding="utf-8").split())
    job.step("transcription", status="done", progress=1.0,
             detail=f"{words:,} mots".replace(",", " "))
    return out


# ----------------------------------------------------------------------
# Étape 2 — synthèse par Claude Code (lit CLAUDE.md dans le dossier du job)
# ----------------------------------------------------------------------
CLAUDE_PROMPT = (
    "La transcription brute d'un cours t'est transmise sur l'entrée standard. "
    "Produis le support de révision complet en suivant strictement les instructions "
    "de CLAUDE.md. Réponds uniquement par le Markdown final, sans préambule ni "
    "commentaire, en commençant directement par la section 1."
)


def summarize(job: Job, transcript: Path) -> Path:
    if not shutil.which("claude"):
        raise RuntimeError(
            "Claude Code est introuvable. Installe-le avec "
            "'npm install -g @anthropic-ai/claude-code' puis connecte-toi avec 'claude'."
        )

    job.step("synthese", status="running", detail="Claude lit la transcription")

    cmd = ["claude", "-p", CLAUDE_PROMPT, "--output-format", "json", "--max-turns", "6"]
    with transcript.open("rb") as stdin:
        proc = subprocess.run(
            cmd, stdin=stdin, capture_output=True, cwd=job.folder, timeout=1800
        )

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
        raise RuntimeError(detail[:400] or "Claude Code a échoué sans message.")

    raw = proc.stdout.decode("utf-8", "replace").strip()
    markdown = raw
    try:
        payload = json.loads(raw)
        markdown = payload.get("result", raw)
        usage = payload.get("usage") or {}
        job.usage = {
            "entree": usage.get("input_tokens"),
            "sortie": usage.get("output_tokens"),
            "duree": payload.get("duration_ms"),
        }
    except json.JSONDecodeError:
        pass

    if not markdown.strip():
        raise RuntimeError("Claude a renvoyé une réponse vide.")

    out = job.folder / f"{job.audio.stem}.md"
    out.write_text(markdown.strip() + "\n", encoding="utf-8")
    job.step("synthese", status="done", progress=1.0,
             detail=f"{len(markdown.split()):,} mots".replace(",", " "))
    return out


# ----------------------------------------------------------------------
# Étape 3 — mise en page PDF
# ----------------------------------------------------------------------
def make_pdf(job: Job, markdown: Path) -> Optional[Path]:
    if not PDF_SCRIPT.exists():
        job.step("pdf", status="skipped", detail="Script de mise en page absent")
        return None

    job.step("pdf", status="running", detail="Rendu Chromium")
    proc = subprocess.run(
        [sys.executable, str(PDF_SCRIPT), str(markdown)],
        capture_output=True, timeout=600,
    )
    pdf = markdown.with_suffix(".pdf")
    if proc.returncode != 0 or not pdf.exists():
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        job.step("pdf", status="skipped",
                 detail=detail[-1][:160] if detail else "Playwright indisponible")
        return None

    size = pdf.stat().st_size / 1024
    job.step("pdf", status="done", progress=1.0, detail=f"{size:.0f} Ko")
    return pdf


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def run_pipeline(job: Job) -> None:
    try:
        transcript = transcribe(job)
    except Exception as exc:  # noqa: BLE001
        job.fail("transcription", str(exc)[:400])
        return
    try:
        markdown = summarize(job, transcript)
    except Exception as exc:  # noqa: BLE001
        job.fail("synthese", str(exc)[:400])
        return
    try:
        make_pdf(job, markdown)
    except Exception as exc:  # noqa: BLE001
        job.step("pdf", status="skipped", detail=str(exc)[:160])

    job.state = "done"
    job.push()


app = FastAPI(title="Atelier de cours")


@app.post("/api/jobs")
async def create_job(
    fichier: UploadFile = File(...),
    matiere: str = Form(""),
    termes: str = Form(""),
    modele: str = Form("large-v3"),
):
    suffix = Path(fichier.filename or "").suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise HTTPException(
            415, f"Format {suffix or 'inconnu'} non pris en charge. "
                 "Utilise MP3, WAV, M4A, OGG, FLAC ou OPUS.")

    job_id = uuid.uuid4().hex[:10]
    stem = Path(fichier.filename).stem.replace(" ", "_")[:60] or "cours"
    folder = JOBS_DIR / f"{time.strftime('%Y-%m-%d')}_{stem}_{job_id}"
    folder.mkdir(parents=True, exist_ok=True)

    audio = folder / f"{stem}{suffix}"
    with audio.open("wb") as fh:
        while chunk := await fichier.read(1 << 20):
            fh.write(chunk)

    if CLAUDE_MD.exists():
        shutil.copy(CLAUDE_MD, folder / "CLAUDE.md")

    job = Job(job_id, folder, audio,
              {"matiere": matiere, "termes": termes, "modele": modele})
    _jobs[job_id] = job
    asyncio.get_event_loop().run_in_executor(None, run_pipeline, job)
    return job.snapshot()


@app.get("/api/jobs/{job_id}/events")
async def events(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Ce cours n'existe pas ou a été effacé au redémarrage.")

    async def stream():
        yield f"data: {json.dumps(job.snapshot(), ensure_ascii=False)}\n\n"
        while True:
            try:
                payload = await asyncio.wait_for(job.queue.get(), timeout=20)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"data: {payload}\n\n"
            if job.state in {"done", "error"}:
                break

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/content/{kind}")
async def content(job_id: str, kind: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Cours introuvable.")
    path = {"md": job.folder / f"{job.audio.stem}.md",
            "txt": job.folder / "transcript.txt"}.get(kind)
    if not path or not path.exists():
        raise HTTPException(404, "Ce fichier n'a pas encore été produit.")
    return {"texte": path.read_text(encoding="utf-8")}


@app.get("/api/jobs/{job_id}/download/{kind}")
async def download(job_id: str, kind: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Cours introuvable.")
    path = {"md": job.folder / f"{job.audio.stem}.md",
            "pdf": job.folder / f"{job.audio.stem}.pdf",
            "txt": job.folder / "transcript.txt"}.get(kind)
    if not path or not path.exists():
        raise HTTPException(404, "Ce fichier n'a pas été produit.")
    return FileResponse(path, filename=path.name)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("ATELIER_PORT", 8765))
    print(f"\n  Atelier de cours  ->  http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
