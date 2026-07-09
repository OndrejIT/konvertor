from __future__ import annotations

import asyncio
import enum
from pathlib import Path
from typing import Callable, NamedTuple

from app.config import settings


class ConversionError(RuntimeError):
    pass


class ConversionMode(str, enum.Enum):
    TO_16_9_BLUR = "to_16_9_blur"
    TO_16_9_BLACK_BARS = "to_16_9_black_bars"


class BlurLevel(str, enum.Enum):
    LIGHT = "light"
    MEDIUM = "medium"
    STRONG = "strong"


class Resolution(str, enum.Enum):
    AUTO = "auto"
    FULL_HD = "1080p"
    HD = "720p"
    UHD_4K = "4k"


class BlurParams(NamedTuple):
    radius: int
    power: int
    brightness: float


BLUR_PRESETS: dict[BlurLevel, BlurParams] = {
    BlurLevel.LIGHT: BlurParams(radius=10, power=4, brightness=-0.15),
    BlurLevel.MEDIUM: BlurParams(radius=20, power=10, brightness=-0.25),
    BlurLevel.STRONG: BlurParams(radius=35, power=12, brightness=-0.35),
}

# Pevná rozlišení (16:9). "AUTO" se dopočítá dynamicky z rozlišení videa.
FIXED_RESOLUTIONS: dict[Resolution, tuple[int, int]] = {
    Resolution.FULL_HD: (1920, 1080),
    Resolution.HD: (1280, 720),
    Resolution.UHD_4K: (3840, 2160),
}

# Bezpečný strop pro AUTO režim, aby extrémně vysoké rozlišení zdroje
# nevygenerovalo nepřiměřeně velké plátno (a zátěž na serveru). Odpovídá
# šířce 4K, což je nejvyšší z pevných předvoleb.
AUTO_MAX_LONG_EDGE = 3840


ProgressCallback = Callable[[float], None]


def _round_even(value: float) -> int:
    n = int(round(value))
    if n % 2:
        n -= 1
    return max(n, 2)


async def probe_duration_seconds(path: Path) -> float | None:
    args = [
        settings.ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await process.communicate()
    except FileNotFoundError:
        return None

    try:
        return float(stdout.decode().strip())
    except ValueError:
        return None


async def probe_resolution(path: Path) -> tuple[int, int] | None:
    args = [
        settings.ffprobe_binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await process.communicate()
    except FileNotFoundError:
        return None

    try:
        width_str, height_str = stdout.decode().strip().split(",")
        return int(width_str), int(height_str)
    except (ValueError, AttributeError):
        return None


def _auto_canvas_from_source(source_width: int, source_height: int) -> tuple[int, int]:
    # Delší hrana zdroje (bez ohledu na orientaci) se mapuje na šířku 16:9
    # plátna - díky tomu vertikální Full HD video (1080x1920, delší hrana
    # 1920) skončí na Full HD plátně (1920x1080), a ne na plátně velkém
    # jako 4K. Výška plátna se dopočítá z poměru 16:9.
    long_edge = _round_even(min(max(source_width, source_height), AUTO_MAX_LONG_EDGE))
    height = _round_even(long_edge * 9 / 16)
    return long_edge, height


async def resolve_canvas_size(resolution: Resolution, input_path: Path) -> tuple[int, int]:
    if resolution is not Resolution.AUTO:
        return FIXED_RESOLUTIONS[resolution]

    probed = await probe_resolution(input_path)
    if probed is None:
        return FIXED_RESOLUTIONS[Resolution.FULL_HD]

    return _auto_canvas_from_source(*probed)


def build_ffmpeg_args(
    mode: ConversionMode,
    input_path: Path,
    output_path: Path,
    canvas_size: tuple[int, int],
    blur: BlurParams,
) -> list[str]:
    width, height = canvas_size

    if mode is ConversionMode.TO_16_9_BLUR:
        video_filter = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur={blur.radius}:{blur.power},"
            f"eq=brightness={blur.brightness}[bg];"
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[vout]"
        )
        filter_args = ["-filter_complex", video_filter, "-map", "[vout]"]
    elif mode is ConversionMode.TO_16_9_BLACK_BARS:
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
        filter_args = ["-vf", video_filter, "-map", "0:v:0"]
    else:
        raise ConversionError(f"Neznámý režim konverze: {mode}")

    return [
        settings.ffmpeg_binary,
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        str(input_path),
        *filter_args,
        # jen první audio stopa, pokud existuje (vynechá datové/titulkové
        # streamy, které by MP4 kontejner nemusel unést)
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-threads",
        str(settings.ffmpeg_threads),
        "-crf",
        "18",
        "-preset",
        "medium",
        # 8bit 4:2:0 - jinak 10bit/HDR zdroje vyrobí profil, který řada
        # přehrávačů nepřehraje
        "-pix_fmt",
        "yuv420p",
        # audio vždy překódovat do AAC - "copy" selže u kodeků, které MP4
        # neumí (Opus/Vorbis z WebM, PCM z MOV)
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        # moov atom na začátek souboru, ať jde video přehrávat hned
        "-movflags",
        "+faststart",
        str(output_path),
    ]


async def convert_video(
    mode: ConversionMode,
    input_path: Path,
    output_path: Path,
    blur_level: BlurLevel = BlurLevel.MEDIUM,
    resolution: Resolution = Resolution.AUTO,
    on_progress: ProgressCallback | None = None,
) -> tuple[int, int]:
    """Zkonvertuje video a vrátí skutečné rozměry výstupního plátna."""
    duration = await probe_duration_seconds(input_path)
    canvas_size = await resolve_canvas_size(resolution, input_path)
    blur = BLUR_PRESETS[blur_level]
    args = build_ffmpeg_args(mode, input_path, output_path, canvas_size, blur)

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_chunks: list[bytes] = []

    async def read_stdout() -> None:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if duration and on_progress and text.startswith("out_time_us="):
                try:
                    out_seconds = int(text.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                percent = max(0.0, min(99.0, out_seconds / duration * 100))
                on_progress(percent)

    async def read_stderr() -> None:
        assert process.stderr is not None
        while True:
            chunk = await process.stderr.readline()
            if not chunk:
                break
            stderr_chunks.append(chunk)

    try:
        await asyncio.wait_for(
            asyncio.gather(read_stdout(), read_stderr(), process.wait()),
            timeout=settings.ffmpeg_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ConversionError("Konverze videa vypršela (timeout).") from exc

    if process.returncode != 0:
        raise ConversionError(
            f"ffmpeg selhal (kód {process.returncode}): "
            f"{b''.join(stderr_chunks).decode(errors='replace')[-2000:]}"
        )

    if on_progress:
        on_progress(100.0)

    return canvas_size
