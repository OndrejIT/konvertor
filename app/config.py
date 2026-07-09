from __future__ import annotations

import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.toml"


@dataclass(frozen=True)
class Settings:
    upload_password: str
    max_size_mb: int
    work_dir: Path
    ffmpeg_binary: str
    ffprobe_binary: str
    ffmpeg_threads: int
    ffmpeg_timeout_seconds: int
    job_ttl_seconds: int
    sweep_interval_seconds: int

    @property
    def max_size_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024


def load_settings() -> Settings:
    with CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)

    security = raw.get("security", {})
    upload = raw.get("upload", {})
    ffmpeg = raw.get("ffmpeg", {})

    configured_work_dir = upload.get("work_dir", "")
    if configured_work_dir:
        work_dir = Path(configured_work_dir).resolve()
    else:
        # Prázdné = systémový temp adresář (na Linuxu je /tmp často tmpfs
        # v RAM; pro jistotu lze v config.toml nastavit work_dir = "/dev/shm").
        work_dir = Path(tempfile.gettempdir()) / "konvertor"
    work_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        upload_password=security["upload_password"],
        max_size_mb=int(upload.get("max_size_mb", 100)),
        work_dir=work_dir,
        ffmpeg_binary=ffmpeg.get("binary", "ffmpeg"),
        ffprobe_binary=ffmpeg.get("probe_binary", "ffprobe"),
        ffmpeg_threads=int(ffmpeg.get("threads", 2)),
        ffmpeg_timeout_seconds=int(ffmpeg.get("timeout_seconds", 1800)),
        job_ttl_seconds=int(upload.get("job_ttl_seconds", 1800)),
        sweep_interval_seconds=int(upload.get("sweep_interval_seconds", 600)),
    )


settings = load_settings()
