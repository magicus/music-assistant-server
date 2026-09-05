"""Python orchestration for on-demand live LMS tests in Docker."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pytest


@dataclass(slots=True, frozen=True)
class LiveLmsEndpoint:
    """Resolved endpoint details for the Docker-managed live LMS."""

    host: str
    port: int
    base_url: str


class LiveLmsError(RuntimeError):
    """Raised when Docker/ffmpeg/LMS orchestration fails."""


VERBOSE_PROGRESS = False


def _set_progress_verbosity(enabled: bool) -> None:
    """Set whether harness progress messages should be printed."""
    global VERBOSE_PROGRESS
    VERBOSE_PROGRESS = enabled


def _progress(message: str, *, force: bool = False) -> None:
    """Print a live progress message for on-demand local runs."""
    if force or VERBOSE_PROGRESS:
        print(f"[lyrion-live] {message}", flush=True)


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO_ROOT / "tests/providers/lyrion/docker-compose.lms.yml"
LMS_BUILD_DIR = REPO_ROOT / "build/test/lyrion"
LMS_CONFIG_DIR = LMS_BUILD_DIR / "config"
LMS_MUSIC_DIR = LMS_BUILD_DIR / "music"

CATALOG: tuple[dict[str, Any], ...] = (
    {
        "artist": "DJ Home Azziztant",
        "album": "Home Sweet Home Lab",
        "genre": "Electro",
        "tracks": (
            ("Wake Up And Smell The Exceptions", 1, 1),
            ("Kiss My Cache", 1, 2),
        ),
    },
    {
        "artist": "DJ Home Azziztant",
        "album": "Cache Me Outside",
        "genre": "Lo-Fi",
        "tracks": (("Cold Start Romance", 1, 1),),
    },
    {
        "artist": "The Async Awaiters",
        "album": "Awaiting Sunrise",
        "genre": "Electro",
        "tracks": (
            ("Await Me Maybe", 1, 1),
            ("Future Is Pending", 1, 2),
        ),
    },
    {
        "artist": "The Async Awaiters",
        "album": "Race Condition Blues",
        "genre": "Blues",
        "tracks": (
            ("Race You To The Lock", 1, 1),
            ("Segfault Serenade", 2, 3),
        ),
    },
    {
        "artist": "One Hit Wonderbread",
        "album": "Greatest Hit And That's It",
        "genre": "Lo-Fi",
        "tracks": (("Breadline Top 1", 1, 1),),
    },
    {
        "artist": "Null Pointer Sisters",
        "album": "None Shall Pass",
        "genre": "Blues",
        "tracks": (
            ("None Shall Dance", 1, 1),
            ("Guard Clause Cha-Cha", 1, 2),
        ),
    },
)


def _safe_filename(value: str) -> str:
    """Normalize a catalog label into a filesystem-safe name."""
    value = value.replace("/", "-").replace(":", "-")
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    return value.strip() or "untitled"


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> str:
    """Run a subprocess command and return combined output on success."""
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return (completed.stdout or "") + (completed.stderr or "")
    except FileNotFoundError as err:
        msg = f"Command not found: {cmd[0]}"
        raise LiveLmsError(msg) from err
    except subprocess.TimeoutExpired as err:
        msg = textwrap.dedent(
            f"""
            Command timed out after {timeout_s}s: {" ".join(cmd)}
            """
        ).strip()
        raise LiveLmsError(msg) from err
    except subprocess.CalledProcessError as err:
        output = ((err.stdout or "") + "\n" + (err.stderr or "")).strip()
        msg = textwrap.dedent(
            f"""
            Command failed: {" ".join(cmd)}
            Exit code: {err.returncode}
            Output:
            {output or "<no output>"}
            """
        ).strip()
        raise LiveLmsError(msg) from err


def _run_with_retries(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float | None = None,
    retries: int = 4,
    delay_s: float = 2.0,
) -> str:
    """Run command with retries for transient Docker race conditions."""
    last_err: LiveLmsError | None = None
    for attempt in range(1, retries + 1):
        try:
            return _run(
                cmd,
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
            )
        except LiveLmsError as err:
            msg = str(err).lower()
            transient = "removal of container" in msg and "already in progress" in msg
            if transient and attempt < retries:
                _progress("docker reports container removal in progress; retrying startup")
                time.sleep(delay_s)
                last_err = err
                continue
            raise

    if last_err is not None:
        raise last_err
    msg = "Unexpected startup retry failure state"
    raise LiveLmsError(msg)


def _run_no_raise(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Run a subprocess command and always return its captured output."""
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return f"Command not found: {cmd[0]}"
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    return output or "<no output>"


def _resolve_compose_cmd() -> list[str]:
    """Resolve docker compose command variant available on the host."""
    if shutil.which("docker"):
        try:
            _run(["docker", "compose", "version"])
            return ["docker", "compose"]
        except LiveLmsError:
            pass
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    msg = "Neither 'docker compose' nor 'docker-compose' is available"
    raise LiveLmsError(msg)


def _json_rpc(
    base_url: str,
    command: list[Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Send one LMS JSON-RPC command and return decoded JSON."""
    payload = {
        "id": 1,
        "method": "slim.request",
        "params": ["", command],
    }
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        f"{base_url}/jsonrpc.js",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _extract_loop_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract LMS loop rows from a result object."""
    for key, value in result.items():
        if key.endswith("_loop") and isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _wait_for_port(host: str, port: int, timeout_s: float = 120.0) -> None:
    """Wait until TCP port becomes reachable."""
    end = time.time() + timeout_s
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(1.0)
    msg = f"Timeout waiting for {host}:{port}"
    raise TimeoutError(msg)


def _wait_for_catalog_ready(base_url: str, timeout_s: float = 240.0) -> None:
    """Wait until expected catalog rows are visible in LMS listings."""
    end = time.time() + timeout_s
    last_counts = "artists=0 albums=0 tracks=0 playlists=0"
    last_reported = ""
    start = time.time()
    while time.time() < end:
        try:
            artists_rsp = _json_rpc(base_url, ["artists", 0, 1000])
            albums_rsp = _json_rpc(base_url, ["albums", 0, 1000])
            tracks_rsp = _json_rpc(base_url, ["titles", 0, 1000])
            playlists_rsp = _json_rpc(base_url, ["playlists", 0, 200])
        except URLError, TimeoutError, OSError, ValueError:
            time.sleep(2.0)
            continue

        artists = _extract_loop_rows(artists_rsp.get("result", {}))
        albums = _extract_loop_rows(albums_rsp.get("result", {}))
        tracks = _extract_loop_rows(tracks_rsp.get("result", {}))
        playlists = _extract_loop_rows(playlists_rsp.get("result", {}))
        last_counts = (
            f"artists={len(artists)} "
            f"albums={len(albums)} "
            f"tracks={len(tracks)} "
            f"playlists={len(playlists)}"
        )
        if last_counts != last_reported:
            elapsed = time.time() - start
            _progress(f"scan progress ({elapsed:.1f}s): {last_counts}")
            last_reported = last_counts

        if len(artists) >= 4 and len(albums) >= 6 and len(tracks) >= 10 and len(playlists) >= 2:
            return

        time.sleep(2.0)

    msg = f"Timeout waiting for LMS catalog readiness. Last observed counts: {last_counts}"
    raise TimeoutError(msg)


def _generate_silent_mp3(
    output_path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    genre: str,
    disc_number: int,
    track_number: int,
) -> None:
    """Generate a 1-second silent MP3 with deterministic ID3 metadata."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=stereo",
        "-t",
        "1",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "9",
        "-metadata",
        f"title={title}",
        "-metadata",
        f"artist={artist}",
        "-metadata",
        f"album={album}",
        "-metadata",
        f"genre={genre}",
        "-metadata",
        f"disc={disc_number}",
        "-metadata",
        f"track={track_number}",
        str(output_path),
    ]
    _run(ffmpeg_cmd)


def _write_catalog(music_dir: Path) -> None:
    """Create deterministic artist/album/track tree plus two M3U playlists."""
    if music_dir.exists():
        shutil.rmtree(music_dir)
    music_dir.mkdir(parents=True, exist_ok=True)

    track_path_map: dict[str, Path] = {}
    for album_spec in CATALOG:
        artist_name = str(album_spec["artist"])
        album_name = str(album_spec["album"])
        genre_name = str(album_spec["genre"])
        for title, disc_number, track_number in album_spec["tracks"]:
            file_name = f"{track_number:02d} - {_safe_filename(title)}.mp3"
            rel_path = Path(_safe_filename(artist_name)) / _safe_filename(album_name) / file_name
            abs_path = music_dir / rel_path
            _generate_silent_mp3(
                abs_path,
                title=title,
                artist=artist_name,
                album=album_name,
                genre=genre_name,
                disc_number=int(disc_number),
                track_number=int(track_number),
            )
            track_path_map[title] = rel_path

    playlists_dir = music_dir / "Playlists"
    playlists_dir.mkdir(parents=True, exist_ok=True)

    debug_bangers = [
        "Wake Up And Smell The Exceptions",
        "Cold Start Romance",
        "Future Is Pending",
        "Breadline Top 1",
    ]
    guard_clauses = [
        "Kiss My Cache",
        "Await Me Maybe",
        "Race You To The Lock",
        "None Shall Dance",
        "Guard Clause Cha-Cha",
    ]

    (playlists_dir / "Debugging Bangers.m3u").write_text(
        ("\n".join(str(track_path_map[title]) for title in debug_bangers) + "\n"),
        encoding="utf-8",
    )
    (playlists_dir / "Guard Clauses Only.m3u").write_text(
        ("\n".join(str(track_path_map[title]) for title in guard_clauses) + "\n"),
        encoding="utf-8",
    )


def _write_server_prefs(config_dir: Path) -> None:
    """Write minimum server prefs to skip first-run wizard in tests."""
    prefs_dir = config_dir / "prefs"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    server_prefs = prefs_dir / "server.prefs"
    server_prefs.write_text(
        "\n".join(
            (
                "wizardDone: 1",
                "protectSettings: 0",
                "audiodir: /music",
                "playlistdir: /music/Playlists",
                "rescaninterval: 0",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def _docker_env() -> dict[str, str]:
    """Return docker compose env for LMS test run."""
    env = os.environ.copy()
    env.setdefault("TZ", "UTC")
    env.setdefault("PUID", str(os.getuid()))
    env.setdefault("PGID", str(os.getgid()))
    env["LMS_CONFIG_DIR"] = str(LMS_CONFIG_DIR)
    env["LMS_MUSIC_DIR"] = str(LMS_MUSIC_DIR)
    return env


def _compose_logs(compose_cmd: list[str]) -> str:
    """Return the latest LMS container logs for diagnostics."""
    return _run_no_raise(
        [
            *compose_cmd,
            "-f",
            str(COMPOSE_FILE),
            "logs",
            "--tail",
            "200",
            "lms",
        ],
        cwd=REPO_ROOT,
        env=_docker_env(),
    )


def _compose_ps(compose_cmd: list[str]) -> str:
    """Return compose service state for diagnostics."""
    return _run_no_raise(
        [
            *compose_cmd,
            "-f",
            str(COMPOSE_FILE),
            "ps",
            "--all",
        ],
        cwd=REPO_ROOT,
        env=_docker_env(),
    )


def _container_unhealthy_state(compose_ps_output: str) -> bool:
    """Return True if compose output indicates exited or restart loop state."""
    lowered = compose_ps_output.lower()
    return "restarting" in lowered or "exited" in lowered or "dead" in lowered


def _bring_up_lms(base_url: str) -> None:
    """Start LMS container and wait until serverstatus works."""
    compose_cmd = _resolve_compose_cmd()
    _progress("starting LMS docker container")
    compose_up_cmd = [
        *compose_cmd,
        "-f",
        str(COMPOSE_FILE),
        "up",
        "-d",
        "--no-recreate",
    ]
    _progress(f"compose command: {' '.join(compose_up_cmd)}")
    _progress("starting container (timeout 120s)")
    try:
        _run_with_retries(
            compose_up_cmd,
            cwd=REPO_ROOT,
            env=_docker_env(),
            timeout_s=120.0,
        )
        _progress("container start command returned")
    except LiveLmsError as err:
        logs = _compose_logs(compose_cmd)
        msg = textwrap.dedent(
            f"""
            Failed to start LMS container.
            {err}

            Container logs (last 200 lines):
            {logs}
            """
        ).strip()
        raise LiveLmsError(msg) from err

    parsed = urlparse(base_url)
    host = str(parsed.hostname)
    port = int(parsed.port) if parsed.port else 9000
    _progress(f"waiting for TCP endpoint {host}:{port}")
    _wait_for_port(host, port)

    end = time.time() + 180.0
    start = time.time()
    next_status_report = time.time() + 5.0
    _progress("waiting for LMS JSON-RPC serverstatus")
    while time.time() < end:
        ps_output = _compose_ps(compose_cmd)
        if _container_unhealthy_state(ps_output):
            logs = _compose_logs(compose_cmd)
            msg = textwrap.dedent(
                f"""
                LMS container is not healthy during startup.
                Compose status:
                {ps_output}

                Container logs (last 200 lines):
                {logs}
                """
            ).strip()
            raise LiveLmsError(msg)

        try:
            rsp = _json_rpc(base_url, ["serverstatus", 0, 1])
            if isinstance(rsp.get("result"), dict):
                _progress("LMS JSON-RPC is ready")
                return
        except URLError, TimeoutError, OSError, ValueError:
            pass

        if time.time() >= next_status_report:
            elapsed = time.time() - start
            _progress(f"still waiting for JSON-RPC ({elapsed:.1f}s)")
            _progress(_compose_ps(compose_cmd))
            next_status_report = time.time() + 5.0

        time.sleep(2.0)

    logs = _compose_logs(compose_cmd)
    ps = _compose_ps(compose_cmd)
    msg = textwrap.dedent(
        f"""
        LMS did not become ready for JSON-RPC in time.
        Endpoint: {base_url}

        Compose status:
        {ps}

        Container logs (last 200 lines):
        {logs}
        """
    ).strip()
    raise TimeoutError(msg)


def _trigger_rescan(base_url: str) -> None:
    """Trigger a library rescan, ignoring unsupported command variants."""
    _progress("triggering LMS rescan")
    for command in (["rescan"], ["rescan", "full"]):
        try:
            _json_rpc(base_url, command)
            _progress(f"rescan command accepted: {command}")
            return
        except URLError, TimeoutError, OSError, ValueError:
            continue


def _bring_down_lms(*, ignore_errors: bool = False) -> None:
    """Stop and remove LMS container for the test harness."""
    compose_cmd = _resolve_compose_cmd()
    _progress("stopping LMS docker container")
    if ignore_errors:
        _run_no_raise(
            [*compose_cmd, "-f", str(COMPOSE_FILE), "down"],
            cwd=REPO_ROOT,
            env=_docker_env(),
        )
        return
    _run(
        [*compose_cmd, "-f", str(COMPOSE_FILE), "down"],
        cwd=REPO_ROOT,
        env=_docker_env(),
    )


@pytest.fixture(scope="session")
def lyrion_live_lms_endpoint(pytestconfig: pytest.Config) -> LiveLmsEndpoint:
    """
    Provide a Docker-managed live LMS endpoint for on-demand integration tests.

    Enable with ``--live-lyrion-docker``. Tests are skipped by default.
    """
    enabled = bool(pytestconfig.getoption("--live-lyrion-docker"))
    if not enabled and os.getenv("LYRION_TEST_DOCKER") != "1":
        pytest.skip("Set --live-lyrion-docker to run Docker-managed live LMS tests")

    verbose_enabled = (
        bool(pytestconfig.getoption("verbose"))
        or bool(pytestconfig.getoption("--live-lyrion-verbose"))
        or os.getenv("LYRION_TEST_DOCKER_VERBOSE") == "1"
    )
    _set_progress_verbosity(verbose_enabled)
    _progress(
        "verbose progress enabled",
        force=verbose_enabled,
    )
    if platform := os.getenv("LMS_PLATFORM"):
        _progress(
            f"docker platform override: {platform}",
            force=verbose_enabled,
        )

    keep_running = (
        bool(pytestconfig.getoption("--live-lyrion-keep-running"))
        or os.getenv("LYRION_TEST_DOCKER_KEEP_RUNNING") == "1"
    )

    if keep_running:
        _progress("keep-running enabled: preserving existing LMS state")
        if not LMS_CONFIG_DIR.exists():
            _progress("no existing config found; creating initial server prefs")
            LMS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _write_server_prefs(LMS_CONFIG_DIR)
        if not LMS_MUSIC_DIR.exists():
            _progress("no existing music catalog found; generating test catalog")
            _write_catalog(LMS_MUSIC_DIR)
            _progress(f"catalog written under {LMS_MUSIC_DIR}")
    else:
        # Ensure no old container keeps file handles into mounted dirs while
        # we rebuild config/music trees for this deterministic test run.
        _bring_down_lms(ignore_errors=True)

        _progress("preparing deterministic music catalog")
        if LMS_CONFIG_DIR.exists():
            _progress("removing stale LMS config directory")
            shutil.rmtree(LMS_CONFIG_DIR)
        LMS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LMS_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        _write_server_prefs(LMS_CONFIG_DIR)
        _write_catalog(LMS_MUSIC_DIR)
        _progress(f"catalog written under {LMS_MUSIC_DIR}")

    base_url = os.getenv("LYRION_TEST_DOCKER_LMS_URL", "http://127.0.0.1:9000")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        msg = "LYRION_TEST_DOCKER_LMS_URL must include scheme, host and port"
        raise ValueError(msg)

    endpoint = LiveLmsEndpoint(
        host=parsed.hostname,
        port=parsed.port,
        base_url=f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
    )

    try:
        _bring_up_lms(endpoint.base_url)
        _trigger_rescan(endpoint.base_url)
        _progress("waiting for catalog to become visible in LMS")
        _wait_for_catalog_ready(endpoint.base_url)
        _progress("catalog is ready; running tests")
        yield endpoint
    finally:
        if not keep_running:
            _bring_down_lms()
        else:
            _progress(
                "keeping LMS container running for manual inspection",
                force=True,
            )
