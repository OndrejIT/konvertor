"""End-to-end testy přes FastAPI TestClient s reálným ffmpeg.

Heslo se bere z načtené konfigurace, takže testy fungují bez ohledu na to,
co je v config.toml nastavené.
"""

from __future__ import annotations

import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

PASSWORD = settings.upload_password


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    """Krátké vertikální video se zvukem vygenerované ffmpegem."""
    path = tmp_path_factory.mktemp("videos") / "sample.mp4"
    subprocess.run(
        [
            settings.ffmpeg_binary, "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x640:duration=1:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def test_index_and_config(client):
    assert client.get("/").status_code == 200
    assert client.get("/config").json() == {"max_size_mb": settings.max_size_mb}


def test_auth_ok(client):
    assert client.post("/auth", data={"password": PASSWORD}).status_code == 204


def test_auth_wrong_password(client):
    assert client.post("/auth", data={"password": "urcite-spatne"}).status_code == 401


def test_auth_non_ascii_password_returns_401_not_500(client):
    # compare_digest na str s diakritikou hází TypeError - ověřuje, že
    # porovnáváme bytes a ne-ASCII heslo nezpůsobí 500
    assert client.post("/auth", data={"password": "heslíčko"}).status_code == 401


def test_create_job_wrong_password(client):
    resp = client.post(
        "/jobs",
        data={"password": "urcite-spatne"},
        files={"file": ("a.mp4", b"xx", "video/mp4")},
    )
    assert resp.status_code == 401


def test_create_job_empty_file(client):
    resp = client.post(
        "/jobs",
        data={"password": PASSWORD},
        files={"file": ("a.mp4", b"", "video/mp4")},
    )
    assert resp.status_code == 400


def test_unknown_job(client):
    assert client.get("/jobs/neexistuje").status_code == 404
    assert client.get("/jobs/neexistuje/download").status_code == 404


def _convert_and_download(client, sample_video, mode: str) -> None:
    with sample_video.open("rb") as f:
        resp = client.post(
            "/jobs",
            data={"password": PASSWORD, "mode": mode, "resolution": "auto"},
            files={"file": ("sample.mp4", f, "video/mp4")},
        )
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    for _ in range(120):
        state = client.get(f"/jobs/{job_id}").json()
        if state["status"] in ("done", "error"):
            break
        time.sleep(0.25)
    assert state["status"] == "done", state

    download = client.get(f"/jobs/{job_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "video/mp4"
    assert len(download.content) > 1000
    # název ke stažení: <původní-stem>-16x9-<výška>p.mp4; zdroj 320x640
    # v režimu auto -> plátno 640x360
    assert 'filename="sample-16x9-360p.mp4"' in download.headers["content-disposition"]

    # opakované stažení funguje (soubor se po prvním stažení nemaže)
    assert client.get(f"/jobs/{job_id}/download").status_code == 200


def test_convert_blur(client, sample_video):
    _convert_and_download(client, sample_video, "to_16_9_blur")


def test_convert_black_bars(client, sample_video):
    _convert_and_download(client, sample_video, "to_16_9_black_bars")
