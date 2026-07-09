from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app.config import settings
from app.converter import BlurLevel, ConversionError, ConversionMode, Resolution, convert_video
from app.jobs import Job, drop_job, get_job, new_job_id, register_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("konvertor")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CHUNK_SIZE = 1024 * 1024  # 1 MB
# Rezerva nad limit videa pro multipart hlavičky/boundaries.
MAX_REQUEST_BYTES = settings.max_size_bytes + 1024 * 1024

# asyncio drží tasky jen slabými referencemi - bez vlastní reference by
# garbage collector mohl sebrat rozběhnutou konverzi.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _sweep_stale_work_dirs() -> None:
    """Smaže zapomenuté adresáře jobů v work_dir starší než job_ttl_seconds.

    Nezávislé na tom, jestli si je appka pamatuje v JOBS - kryje i případ,
    že by proces mezitím restartoval a přišel o interní evidenci. Aktivní
    (nedokončené) joby se ale nemažou, i kdyby konverze trvala déle než TTL.
    """
    cutoff = time.time() - settings.job_ttl_seconds
    for entry in settings.work_dir.iterdir():
        if not entry.is_dir():
            continue

        job = get_job(entry.name)
        if job is not None and job.status in ("uploaded", "converting"):
            continue

        try:
            is_stale = entry.stat().st_mtime < cutoff
        except FileNotFoundError:
            continue

        if is_stale:
            shutil.rmtree(entry, ignore_errors=True)
            drop_job(entry.name)
            logger.info("sweep: smazán vypršelý job %s", entry.name)


async def _stale_work_dirs_sweeper() -> None:
    while True:
        await asyncio.sleep(settings.sweep_interval_seconds)
        _sweep_stale_work_dirs()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Úklid po případném pádu předchozího běhu. Záměrně NE plošné smazání:
    # work_dir může sdílet víc instancí (dev server + testy) a slepý výmaz
    # by sestřelil rozběhnutou konverzi té druhé. Stejný sweep jako za
    # běhu maže jen adresáře starší než TTL, což je bezpečné.
    _sweep_stale_work_dirs()
    sweeper = _spawn(_stale_work_dirs_sweeper())
    yield
    sweeper.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweeper


app = FastAPI(title="Video Konvertor", lifespan=lifespan)


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/jobs":
        content_length = request.headers.get("content-length")
        # Bez Content-Length by streamované (chunked) tělo obešlo limit -
        # multipart parser by ho stihl celé zapsat do temp souboru dřív,
        # než se dostane ke slovu naše kontrola velikosti.
        if content_length is None:
            return PlainTextResponse(
                "Chybí hlavička Content-Length.",
                status_code=status.HTTP_411_LENGTH_REQUIRED,
            )
        try:
            length = int(content_length)
        except ValueError:
            return PlainTextResponse(
                "Neplatná hlavička Content-Length.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if length > MAX_REQUEST_BYTES:
            return PlainTextResponse(
                f"Požadavek je větší než povolený limit {settings.max_size_mb} MB.",
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )
    return await call_next(request)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/config")
async def public_config() -> JSONResponse:
    return JSONResponse({"max_size_mb": settings.max_size_mb})


# Neúspěšné pokusy o heslo čekají v globální frontě se zpožděním - hrubá
# síla je tak omezená na ~1 pokus/s bez ohledu na počet spojení.
_failed_auth_lock = asyncio.Lock()


async def _check_password(password: str) -> None:
    # encode() - compare_digest neumí str s ne-ASCII znaky (heslo s
    # diakritikou by jinak shodilo endpoint na 500)
    if hmac.compare_digest(password.encode(), settings.upload_password.encode()):
        return
    async with _failed_auth_lock:
        await asyncio.sleep(1.0)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Neplatné heslo.")


@app.post("/auth")
async def verify_password(password: str = Form(...)) -> Response:
    """Ověření hesla bez uploadu - frontend volá před začátkem fronty."""
    await _check_password(password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _save_upload_with_limit(upload: UploadFile, destination: Path) -> None:
    written = 0
    with destination.open("wb") as out_file:
        while chunk := await upload.read(CHUNK_SIZE):
            written += len(chunk)
            if written > settings.max_size_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Video je větší než povolený limit {settings.max_size_mb} MB.",
                )
            out_file.write(chunk)

    if written == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nahraný soubor je prázdný.")


# Konverze běží vždy jen jedna naráz - další joby čekají ve frontě (stav
# "uploaded"), aby si víc souběžných ffmpeg procesů nekonkurovalo o CPU.
_conversion_lock = asyncio.Lock()


async def _run_conversion(
    job: Job,
    mode: ConversionMode,
    input_path: Path,
    blur_level: BlurLevel,
    resolution: Resolution,
) -> None:
    async with _conversion_lock:
        await _run_conversion_locked(job, mode, input_path, blur_level, resolution)


async def _run_conversion_locked(
    job: Job,
    mode: ConversionMode,
    input_path: Path,
    blur_level: BlurLevel,
    resolution: Resolution,
) -> None:
    job.status = "converting"
    started = time.monotonic()
    logger.info("job %s: konverze začala (mode=%s, blur=%s, resolution=%s)",
                job.id, mode.value, blur_level.value, resolution.value)
    try:
        await convert_video(
            mode,
            input_path,
            job.output_path,
            blur_level=blur_level,
            resolution=resolution,
            on_progress=lambda p: setattr(job, "progress", p),
        )
    except ConversionError as exc:
        job.status = "error"
        job.error = str(exc)
        logger.error("job %s: konverze selhala: %s", job.id, exc)
        return
    except Exception as exc:  # noqa: BLE001 - chceme chybu dostat do stavu jobu, ne jen do logu
        job.status = "error"
        job.error = str(exc)
        logger.exception("job %s: neočekávaná chyba konverze", job.id)
        return

    job.progress = 100.0
    job.status = "done"
    logger.info("job %s: hotovo za %.1f s", job.id, time.monotonic() - started)


@app.post("/jobs")
async def create_job(
    password: str = Form(...),
    mode: ConversionMode = Form(ConversionMode.TO_16_9_BLUR),
    blur: BlurLevel = Form(BlurLevel.MEDIUM),
    resolution: Resolution = Form(Resolution.AUTO),
    file: UploadFile = File(...),
) -> JSONResponse:
    await _check_password(password)

    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Chybí soubor.")

    safe_name = Path(file.filename).name
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Neplatný název souboru.")

    job_id = new_job_id()
    work_dir = settings.work_dir / job_id
    input_path = work_dir / "input" / safe_name
    input_path.parent.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / f"konvert-{Path(safe_name).stem}.mp4"

    job = register_job(job_id, work_dir, output_path.name)
    job.output_path = output_path

    try:
        await _save_upload_with_limit(file, input_path)
    except HTTPException:
        shutil.rmtree(work_dir, ignore_errors=True)
        drop_job(job_id)
        raise

    logger.info("job %s: přijat soubor '%s' (%.1f MB)",
                job_id, safe_name, input_path.stat().st_size / (1024 * 1024))
    _spawn(_run_conversion(job, mode, input_path, blur, resolution))

    return JSONResponse({"job_id": job_id}, status_code=status.HTTP_202_ACCEPTED)


@app.get("/jobs/{job_id}")
async def job_status(job_id: str) -> JSONResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job nenalezen (možná už vypršel).")

    return JSONResponse(
        {
            "status": job.status,
            "progress": round(job.progress, 1),
            "error": job.error,
        }
    )


@app.get("/jobs/{job_id}/download")
async def job_download(job_id: str) -> FileResponse:
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job nenalezen (možná už vypršel).")

    if job.status != "done" or job.output_path is None or not job.output_path.exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Video ještě není připravené.")

    # Stažení obnoví TTL (sweep měří stáří podle mtime adresáře) - právě
    # stahovaný job tak nemůže vypršet uprostřed přenosu a "Stáhnout znovu"
    # funguje ještě job_ttl_seconds od posledního stažení.
    with contextlib.suppress(OSError):
        os.utime(job.work_dir)

    return FileResponse(
        job.output_path,
        media_type="video/mp4",
        filename=job.download_name,
    )
