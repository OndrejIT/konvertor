from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

JobStatus = Literal["uploaded", "converting", "done", "error"]


@dataclass
class Job:
    id: str
    work_dir: Path
    download_name: str
    status: JobStatus = "uploaded"
    progress: float = 0.0
    error: str | None = None
    output_path: Path | None = None
    created_at: float = field(default_factory=time.time)


JOBS: dict[str, Job] = {}


def new_job_id() -> str:
    return uuid.uuid4().hex


def register_job(job_id: str, work_dir: Path, download_name: str) -> Job:
    job = Job(id=job_id, work_dir=work_dir, download_name=download_name)
    JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def drop_job(job_id: str) -> None:
    JOBS.pop(job_id, None)
