#!/usr/bin/env python3
"""
pyks2 — Pentax K-S2 WiFi control library.

Camera-only layer for the photogrammetry rig: HTTP API client, media
ingest helpers, trigger interfaces, and the ScanSession capture loop.
Turntable integration lives in controller.py (TurntableTrigger), keeping
this module free of serial/hardware dependencies.

Key protocol facts (confirmed against the C# reference client
pentaxks2wifiremote, PtxK-S2/K-S2.cs):

1. The K-S2's HTTP server is one-request-per-socket. Every request is
   sent with `Connection: close` on a fresh `requests.Session()`. With
   keep-alive, GET /v1/photos hangs indefinitely (the camera enters an
   orange-LED card-read mode that kills WiFi) — the fresh-connection
   approach fixed this, but the endpoint can still take several seconds
   on a full card; latest/info remains the preferred per-shot path.
2. `Content-Type: text/xml` is sent on every request (even GET — the
   C# helper does the same).
3. After `shoot()`, GET /v1/props is polled for `state == "idle"` before
   returning control.
4. `get_latest_info()` hits GET /v1/photos/latest/info — a special-cased
   fast path that avoids the full filelist entirely.
5. Downloads require `?size=full` (the C# urlGetFile does this).

Endpoints:
    POST /v1/camera/shoot           body: af=auto|on|off    Content-Type: text/xml
    GET  /v1/props                                          camera state + params
    PUT  /v1/params/camera          query params            set av/tv/sv/etc.
    GET  /v1/photos                                         full filelist (slow; see note 1)
    GET  /v1/photos/latest/info                             metadata for last shot
    GET  /v1/photos/{dir}/{file}?size=full                  download image
    GET  /v1/photos/latest?size=full                        download latest image directly
    GET  /v1/liveview                                       MJPEG stream

Install: pip install requests
"""

import json
import os
import shutil
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


# ---------------------------------------------------------------------------
# Verbosity & logging
# ---------------------------------------------------------------------------
# All informational output goes through these helpers so we can globally
# silence HTTP traces in production runs while keeping detailed logs in
# scan.log files. Set pyks2.VERBOSE = True at startup to see everything.

VERBOSE = False  # set by controller via --verbose

def _v(msg: str) -> None:
    """Print only if VERBOSE is on. Used for HTTP traces, state polls, etc."""
    if VERBOSE:
        print(msg)

def _info(msg: str) -> None:
    """Always print — for important results (filenames, status, summaries)."""
    print(msg)


# ---------------------------------------------------------------------------
# Low-level HTTP — designed to mimic the C# helper exactly.
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    # C# http.cs sets ContentType="text/xml" on every WebRequest, including GET.
    # The K-S2 firmware appears to accept this without complaint and it's what
    # the camera was tested against in the field, so we mirror it.
    "Content-Type": "text/xml",
    # Force the socket closed after each response. This is the main fix.
    "Connection": "close",
    # No keep-alive negotiation.
    "Accept-Encoding": "identity",
}


def _raw_request(
    method: str,
    url: str,
    body: str = "",
    timeout: float = 15.0,
    stream: bool = False,
    fresh_session: bool = True,
) -> requests.Response:
    """One-shot HTTP request, no connection reuse.

    `fresh_session=True` builds a brand-new Session, makes the call, then closes
    it. This guarantees no socket reuse across method calls, which is the
    behaviour we want for the K-S2.
    """
    if fresh_session:
        with requests.Session() as s:
            s.headers.update(DEFAULT_HEADERS)
            return s.request(method, url, data=body, timeout=timeout, stream=stream)
    else:
        return requests.request(
            method, url, data=body, headers=DEFAULT_HEADERS,
            timeout=timeout, stream=stream,
        )


# ---------------------------------------------------------------------------
# Camera class
# ---------------------------------------------------------------------------

class K_S2_WiFi:
    """Pentax K-S2 WiFi control via HTTP API."""

    SHOOT_URL     = "http://{}/v1/camera/shoot"
    PROPS_URL     = "http://{}/v1/props"
    PARAMS_URL    = "http://{}/v1/params/camera"
    FILELIST_URL  = "http://{}/v1/photos"
    FILE_URL      = "http://{}/v1/photos/{}?size={}"        # path, size
    LATEST_URL    = "http://{}/v1/photos/latest?size={}"    # size
    LATEST_INFO   = "http://{}/v1/photos/latest/info"
    LIVEVIEW_URL  = "http://{}/v1/liveview"

    def __init__(self, camera_ip: str = "192.168.0.1", timeout: float = 15.0):
        self.camera_ip = camera_ip
        self.timeout = timeout
        self.params: Dict[str, Any] = {}
        self.files: List[Dict[str, str]] = []

    # ---- connection -------------------------------------------------------

    def connect(self) -> bool:
        """Verify reachability by fetching /v1/props."""
        try:
            r = _raw_request("GET", self.PROPS_URL.format(self.camera_ip),
                             timeout=self.timeout)
            if r.status_code == 200:
                self.params = r.json()
                print(f"[OK] Connected to K-S2 at {self.camera_ip} "
                      f"(state={self.params.get('state')}, "
                      f"battery={self.params.get('battery')}, "
                      f"fw={self.params.get('firmwareVersion')})")
                return True
            print(f"[ERR] HTTP {r.status_code} on /v1/props")
            return False
        except Exception as e:
            print(f"[ERR] Connection failed: {e}")
            return False

    def close(self):
        """No-op (sessions are per-call now)."""
        pass

    # ---- state ------------------------------------------------------------

    def get_state(self) -> Optional[str]:
        """Return camera 'state' field from /v1/props ('idle', 'capturing', etc.)."""
        try:
            r = _raw_request("GET", self.PROPS_URL.format(self.camera_ip),
                             timeout=self.timeout)
            if r.status_code == 200:
                self.params = r.json()
                return self.params.get("state")
        except Exception as e:
            _v(f"  get_state error: {e}")
        return None

    def get_focus_mode(self) -> Optional[str]:
        """Return camera 'focusMode' field from /v1/props.

        The K-S2 reports 'af' when the body's AF/MF switch is set to AF, and
        'mf' when set to MF. This is a *physical* switch — software cannot
        change it, only read it. Returns None if unreachable.
        """
        try:
            r = _raw_request("GET", self.PROPS_URL.format(self.camera_ip),
                             timeout=self.timeout)
            if r.status_code == 200:
                self.params = r.json()
                return self.params.get("focusMode")
        except Exception as e:
            _v(f"  get_focus_mode error: {e}")
        return None

    def wait_for_idle(self, max_wait: float = 30.0, poll_interval: float = 0.5) -> bool:
        """Poll /v1/props until state == 'idle' or timeout. Returns True if idle."""
        start = time.time()
        last_state = None
        while time.time() - start < max_wait:
            state = self.get_state()
            if state != last_state:
                _v(f"  state: {state}  (t+{time.time()-start:.1f}s)")
                last_state = state
            if state == "idle":
                return True
            time.sleep(poll_interval)
        print(f"[ERR] wait_for_idle timed out after {max_wait}s (last state={last_state})")
        return False

    # ---- shooting ---------------------------------------------------------

    def shoot(self, af: Optional[str] = None, wait_idle: bool = True) -> bool:
        """Trigger shutter. If wait_idle, poll until camera returns to idle.

        af:
            None (default) — auto-detect from the body's physical AF/MF switch.
                MF -> af="off" (so the camera fires without hunting for focus;
                this is the fix for the "won't trigger in MF" bug). AF or any
                other reported mode -> af="auto".
            "auto"|"on"|"off" — force that value, skipping detection.
        """
        if af is None:
            mode = self.get_focus_mode()
            if mode is None:
                # Could not read focusMode. Default to af=off, NOT af=auto:
                # on a manual-focus lens af=auto makes the camera hunt for a
                # focus lock and REFUSE to release the shutter if it can't get
                # one (AF-priority) — the first frame silently never fires.
                # af=off always releases the shutter at the current focus,
                # which is exactly right for a fixed-focus turntable rig.
                af = "off"
                print("[WARN] Could not read focusMode; defaulting to af=off "
                      "(MF-safe; shutter always releases)")
            elif str(mode).lower() == "mf":
                af = "off"
                _v("  focusMode=mf -> af=off")
            else:
                af = "auto"
                _v(f"  focusMode={mode} -> af=auto")
        url = self.SHOOT_URL.format(self.camera_ip)
        body = f"af={af}"
        try:
            r = _raw_request("POST", url, body=body, timeout=self.timeout)
            _v(f"  POST {url} '{body}' -> HTTP {r.status_code}")
            if r.status_code not in (200, 201, 204):
                _v(f"  body: {r.text[:200]}")
                return False
            print(f"[OK] Shutter triggered (af={af})")
        except Exception as e:
            print(f"[ERR] Shoot error: {e}")
            return False

        if wait_idle:
            return self.wait_for_idle(max_wait=30.0)
        return True

    # ---- filelist ----------------------------------------------------------

    def get_filelist(self, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """GET /v1/photos — full SD-card filelist.

        Works with the fresh-connection-per-request approach (hangs the
        camera under keep-alive). Can take several seconds on a full card,
        so it's used only for scan-session baselines/diffs — never inside
        the per-shot loop (use get_latest_info() there)."""
        url = self.FILELIST_URL.format(self.camera_ip)
        t = timeout if timeout is not None else max(self.timeout, 30.0)
        try:
            r = _raw_request("GET", url, timeout=t)
            _v(f"  GET {url} -> HTTP {r.status_code} ({len(r.content)} bytes)")
            if r.status_code != 200:
                return []
            data = r.json()
            all_files = []
            for d in data.get("dirs", []):
                dname = d.get("name", "")
                for fname in d.get("files", []):
                    all_files.append({"dir": dname, "name": fname,
                                      "path": f"{dname}/{fname}"})
            self.files = all_files
            print(f"[OK] Filelist returned {len(all_files)} file(s) across "
                  f"{len(data.get('dirs', []))} dir(s)")
            return all_files
        except requests.exceptions.ReadTimeout:
            print(f"[ERR] Filelist read timeout after {t}s — this is the known bug.")
            print(f"  Workaround: use get_latest_info() / download_latest() instead.")
            return []
        except Exception as e:
            print(f"[ERR] Filelist error: {e}")
            return []

    # ---- /latest workaround (NEW) -----------------------------------------

    def get_latest_info(self, retries: int = 10, retry_delay: float = 0.5) -> Dict[str, Any]:
        """GET /v1/photos/latest/info — metadata for most recent shot.

        Camera reports state='idle' before SD write is fully indexed, so this
        polls until `dir` and `file` are populated (up to retries*retry_delay
        seconds). Returns the full info dict on success, {} on failure.

        Side effect: sets self._info_attempts to the number of polls used so
        callers can log it for stress-test analysis.
        """
        url = self.LATEST_INFO.format(self.camera_ip)
        last_info: Dict[str, Any] = {}
        self._info_attempts = 0
        for attempt in range(retries):
            self._info_attempts = attempt + 1
            try:
                r = _raw_request("GET", url, timeout=self.timeout)
                if attempt == 0:
                    _v(f"  GET {url} -> HTTP {r.status_code}")
                if r.status_code != 200:
                    _v(f"  body: {r.text[:300]}")
                    return {}
                info = r.json()
                last_info = info
                if info.get("dir") and info.get("file"):
                    print(f"  -> dir={info.get('dir')} file={info.get('file')} "
                          f"captured={info.get('captured')}  "
                          f"(attempt {attempt+1}/{retries})")
                    return info
                # still indexing — short backoff
                time.sleep(retry_delay)
            except Exception as e:
                print(f"[ERR] get_latest_info error: {e}")
                return {}
        print(f"[ERR] /latest/info never populated dir/file after "
              f"{retries*retry_delay:.1f}s.  Last response: {last_info}")
        return last_info

    def download_latest(self, output_path: str = "./latest.jpg",
                        size: str = "full") -> bool:
        """Download the most recent photo.

        The camera does NOT accept /v1/photos/latest?size=... as a download URL
        (returns 400). The C# reference only special-cases 'latest' for
        /info — actual download must use the real dir/file path. So we:
          1) call get_latest_info() to resolve dir/file
          2) GET /v1/photos/{dir}/{file}[?size=full]
          3) if that 400s, retry without ?size= (DNGs may not accept size)
        """
        info = self.get_latest_info()
        d, f = info.get("dir"), info.get("file")
        if not (d and f):
            print(f"[ERR] Could not resolve latest dir/file from /latest/info")
            return False
        path = f"{d}/{f}"

        # If the resolved file is a DNG, try with size first, then without.
        # If it's a JPG, ?size=full should always work.
        attempts = [
            ("with ?size=" + size, self.FILE_URL.format(self.camera_ip, path, size)),
            ("no ?size=",          f"http://{self.camera_ip}/v1/photos/{path}"),
        ]
        # If user picked a non-jpg output ext, adjust to match the source ext
        src_ext = os.path.splitext(f)[1].lower()
        out = output_path
        if src_ext and not output_path.lower().endswith(src_ext):
            out = os.path.splitext(output_path)[0] + src_ext
            _v(f"  output extension adjusted: {output_path} -> {out}")

        for label, url in attempts:
            _v(f"  trying {label}: {url}")
            ok = self._download(url, out)
            if ok:
                return True
        return False

    # ---- file download ----------------------------------------------------

    def download_image(self, image_path: str, output_path: str,
                       size: str = "full") -> bool:
        """Download a specific image. `image_path` is 'DIR/FILE'."""
        url = self.FILE_URL.format(self.camera_ip, image_path, size)
        return self._download(url, output_path)

    def _download(self, url: str, output_path: str) -> bool:
        try:
            r = _raw_request("GET", url, timeout=max(self.timeout, 60.0),
                             stream=True)
            if r.status_code != 200:
                print(f"[ERR] Download {url} -> HTTP {r.status_code}")
                return False
            # Camera sometimes returns 200 OK with a JSON error body like
            # {"errCode":400,"errMsg":"Bad Request"}. Sniff content-type and
            # the first bytes before writing.
            ctype = (r.headers.get("Content-Type") or "").lower()
            first = next(r.iter_content(64), b"")
            if b'"errCode"' in first or "json" in ctype:
                print(f"[ERR] Download got JSON error body, not an image:")
                print(f"  Content-Type: {ctype}")
                print(f"  Body: {first.decode('utf-8', 'replace')[:200]}")
                return False
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            n = 0
            with open(output_path, "wb") as f:
                f.write(first)
                n += len(first)
                for chunk in r.iter_content(8192):
                    if chunk:
                        f.write(chunk)
                        n += len(chunk)
            print(f"[OK] Downloaded {Path(output_path).name} ({n:,} bytes)")
            return n > 1000  # anything under 1 KB is almost certainly junk
        except Exception as e:
            print(f"[ERR] Download error: {e}")
            return False

    # ---- params -----------------------------------------------------------

    def get_parameters(self) -> Dict[str, Any]:
        try:
            r = _raw_request("GET", self.PROPS_URL.format(self.camera_ip),
                             timeout=self.timeout)
            if r.status_code == 200:
                self.params = r.json()
                return self.params
        except Exception as e:
            print(f"[ERR] Get params error: {e}")
        return {}

    def set_parameters(self, **kwargs) -> bool:
        # C# uses PUT with the params in the body as a key=value string, not
        # URL params. Mirror that.
        body = "&".join(f"{k}={v}" for k, v in kwargs.items())
        url = self.PARAMS_URL.format(self.camera_ip)
        try:
            r = _raw_request("PUT", url, body=body, timeout=self.timeout)
            if r.status_code in (200, 201):
                if r.text:
                    try:
                        self.params.update(r.json())
                    except Exception:
                        pass
                print(f"[OK] Parameters set: {body}")
                return True
            print(f"[ERR] Set params HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"[ERR] Set params error: {e}")
        return False


# ---------------------------------------------------------------------------
# Offline media ingest — SD card or USB MSC.
# ---------------------------------------------------------------------------

# File extensions we treat as photogrammetry-worthy sources.
PHOTO_EXTS = {".dng", ".pef", ".jpg", ".jpeg", ".tif", ".tiff"}


class MediaIngest:
    """Copy files off a mounted SD card / USB MSC volume into a session dir.

    Why mtime instead of filename matching:
        The K-S2 WiFi API and the actual SD card disagree on filenames.
        Over WiFi, RAW shots appear as IMGP####.DNG with one numbering.
        On disk they're stored as IMGP####.PEF (and IMGP####.JPG if the
        camera is in RAW+JPG mode), with completely different numbering.
        So we can't match by name — we use modification time instead.
        Count from the WiFi filelist diff (which IS reliable) is used as a
        sanity check.
    """

    @staticmethod
    def find_dcim(search_roots: Optional[List[str]] = None,
                  verbose: bool = True) -> Optional[Path]:
        """Find the live DCIM directory on a mounted volume.

        Rules:
          - Folder name must be exactly 'DCIM' (case-insensitive).
            Rejects DCIM-bk, DCIM_old, DCIM.bak, etc.
          - Must contain at least one Pentax-style subfolder (e.g. 100_PENTAX,
            100_2005) to confirm it's a camera card, not random.
          - If multiple matching DCIMs exist on different volumes, picks the
            one whose newest file is most recent (i.e. the "live" card).
        """
        roots: List[Path] = []
        if search_roots:
            roots = [Path(p) for p in search_roots]
        elif os.name == "nt":
            for letter in string.ascii_uppercase[3:]:  # skip A,B,C
                p = Path(f"{letter}:/")
                if p.exists():
                    roots.append(p)
        else:
            for parent in ("/Volumes", "/media", "/mnt", "/run/media"):
                pp = Path(parent)
                if pp.exists():
                    for child in pp.iterdir():
                        if child.is_dir():
                            roots.append(child)
                            try:
                                for grand in child.iterdir():
                                    if grand.is_dir():
                                        roots.append(grand)
                            except (PermissionError, OSError):
                                pass

        # Find every folder *directly named* DCIM under each root.
        candidates: List[Path] = []
        for r in roots:
            try:
                for child in r.iterdir():
                    if child.is_dir() and child.name.upper() == "DCIM":
                        candidates.append(child)
                    elif child.is_dir() and "DCIM" in child.name.upper() \
                            and child.name.upper() != "DCIM":
                        if verbose:
                            print(f"  (ignoring backup-looking folder: {child})")
            except (PermissionError, OSError):
                continue

        # Filter to DCIMs that contain a Pentax-style subdir (1??_*).
        def looks_like_pentax(dcim: Path) -> bool:
            try:
                for sub in dcim.iterdir():
                    if not sub.is_dir():
                        continue
                    n = sub.name
                    # Pentax pattern: 3 digits then _ then anything (100_PENTAX, 100_2005)
                    if len(n) >= 5 and n[:3].isdigit() and n[3] == "_":
                        return True
            except (PermissionError, OSError):
                pass
            return False

        valid = [c for c in candidates if looks_like_pentax(c)]
        if not valid:
            return None

        # If multiple, pick the one with the most recently modified file inside.
        def newest_mtime(dcim: Path) -> float:
            best = 0.0
            for root, _d, files in os.walk(dcim):
                for fn in files:
                    try:
                        m = (Path(root) / fn).stat().st_mtime
                        if m > best:
                            best = m
                    except OSError:
                        pass
            return best

        if len(valid) == 1:
            chosen = valid[0]
        else:
            chosen = max(valid, key=newest_mtime)
            if verbose:
                print(f"  Multiple DCIMs found, picked newest: {chosen}")

        if verbose:
            print(f"  Found DCIM at: {chosen}")
        return chosen

    @staticmethod
    def list_source_files(dcim: Path) -> List[Path]:
        """List all photo-ish files under a DCIM tree, sorted by mtime ascending."""
        out: List[Path] = []
        for root, _dirs, files in os.walk(dcim):
            for f in files:
                if Path(f).suffix.lower() in PHOTO_EXTS:
                    out.append(Path(root) / f)
        out.sort(key=lambda p: p.stat().st_mtime)
        return out

    @staticmethod
    def files_matching_stems(dcim: Path, stems: set) -> List[Path]:
        """Return source files whose filename stem (no extension) is in `stems`.

        Stems are matched case-insensitively. This is the elegant path: WiFi
        reports e.g. 'IMGP9295.DNG' but the SD card stores 'IMGP9295.PEF' +
        'IMGP9295.JPG' (RAW+JPG mode). Matching by stem 'IMGP9295' grabs both.
        """
        wanted = {s.upper() for s in stems}
        out: List[Path] = []
        for p in MediaIngest.list_source_files(dcim):
            if p.stem.upper() in wanted:
                out.append(p)
        return out

    @staticmethod
    def files_newer_than(dcim: Path, cutoff_mtime: float) -> List[Path]:
        """Return source files with mtime strictly greater than cutoff.
        Fallback when stem matching finds nothing (e.g. WiFi numbering
        genuinely doesn't match disk numbering on some firmware).
        """
        return [p for p in MediaIngest.list_source_files(dcim)
                if p.stat().st_mtime > cutoff_mtime]

    @staticmethod
    def newest_n(dcim: Path, n: int) -> List[Path]:
        """Return the n most-recently-modified source files (newest last)."""
        all_files = MediaIngest.list_source_files(dcim)
        return all_files[-n:] if n < len(all_files) else all_files

    @staticmethod
    def copy_files(src_files: List[Path], dest_dir: Path,
                   verbose: bool = True) -> Dict[str, Any]:
        """Copy a specific list of source paths to dest_dir.
        Skips files that already exist with matching size.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        skipped = 0
        total_bytes = 0
        t0 = time.time()
        for src in src_files:
            dst = dest_dir / src.name
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                if verbose:
                    print(f"  skip  {src.name} (already present)")
                skipped += 1
                continue
            shutil.copy2(src, dst)
            sz = dst.stat().st_size
            total_bytes += sz
            copied += 1
            if verbose:
                print(f"  copy  {src.name} ({sz:,} bytes)")
        elapsed = time.time() - t0
        mb = total_bytes / (1024 * 1024)
        rate = mb / elapsed if elapsed > 0 else 0
        if verbose:
            print(f"  done: {copied} copied, {skipped} skipped, "
                  f"{mb:.1f} MB in {elapsed:.1f}s ({rate:.1f} MB/s)")
        return {"copied": copied, "skipped": skipped,
                "bytes": total_bytes, "elapsed_s": round(elapsed, 2),
                "mb_per_sec": round(rate, 1)}


# ---------------------------------------------------------------------------
# Triggers — pacing interface for ScanSession. The turntable-driven
# implementation (TurntableTrigger) lives in controller.py so this module
# stays camera-only.
# ---------------------------------------------------------------------------

class Trigger:
    """Base class. wait() blocks until it's time to fire the next shot."""
    def wait(self, shot_index: int, total: int) -> bool:
        """Return True to proceed with the shot, False to abort the scan."""
        raise NotImplementedError

    def close(self):
        pass


class ManualTrigger(Trigger):
    """Press Enter to fire next shot. 'q' aborts. Empty Enter = fire."""
    def wait(self, shot_index: int, total: int) -> bool:
        try:
            r = input(f"  [shot {shot_index+1}/{total}] Enter=shoot, q=abort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if r == "q":
            return False
        return True


# ---------------------------------------------------------------------------
# ScanSession — orchestrates a full multi-shot capture + post-scan download.
# ---------------------------------------------------------------------------

class ScanSession:
    """One photogrammetry scan: trigger -> shoot -> log; download diff after.

    Lifecycle:
        s = ScanSession(cam, trigger=ManualTrigger(), root='./scans')
        s.run(n_shots=36)              # captures + downloads
        # files end up in ./scans/{timestamp}/IMGP*.DNG
        # log alongside them in scan.log
    """

    def __init__(self, cam: "K_S2_WiFi",
                 trigger: Optional[Trigger] = None,
                 root: str = "./scans",
                 use_timestamped_subdir: bool = True):
        """
        Args:
            cam: connected K_S2_WiFi camera
            trigger: a Trigger instance (default ManualTrigger)
            root: directory to write scan output into
            use_timestamped_subdir: if True (default), creates a
                `{root}/{timestamp}/` subfolder for this scan's output.
                If False, writes directly into `root`. Set False when the
                caller has already created a properly-named scan dir.
        """
        self.cam = cam
        self.trigger = trigger or ManualTrigger()
        self.root = root
        self.use_timestamped_subdir = use_timestamped_subdir
        self.session_dir: Optional[Path] = None
        self.before_files: set = set()
        self.shot_log: List[Dict[str, Any]] = []  # entries per shot
        self.session_start_mtime: float = 0.0
        self.session_end_mtime: Optional[float] = None

    def _snapshot_filelist(self) -> set:
        """Return set of 'dir/file' paths currently on the camera."""
        files = self.cam.get_filelist()
        return {f["path"] for f in files}

    def _new_files_since_start(self) -> List[str]:
        after = self._snapshot_filelist()
        new = sorted(after - self.before_files)
        return new

    def run(self, n_shots: int, af: Optional[str] = None,
            download_mode: str = "wifi",
            check_format_per_shot: bool = False) -> Dict[str, Any]:
        """Execute the scan.

        af:
            None (default) — auto-detect AF/MF from the camera body per shot.
            "auto"|"on"|"off" — force that value for every shot.

        download_mode:
            "wifi"   - download via HTTP after capture (slow, ~60s/DNG)
            "ingest" - skip WiFi download; prompt user to plug in SD/USB
                       after capture, then copy matching files locally
            "defer"  - capture only, write session.json with state=pending_ingest.
                       Caller is responsible for running ingest later (e.g. a
                       project-level ingest pass after multiple scans).
            "none"   - capture only, no download. Files remain on card.
                       Returned summary lists expected filenames for later.

        check_format_per_shot:
            If True, polls /v1/props after every shot to capture
            storages[0].format. Useful for catching the K-S2's known
            silent RAW+JPG → RAW format flip. Adds ~100ms per shot.

        Returns summary dict.
        """
        if download_mode not in ("wifi", "ingest", "defer", "none"):
            raise ValueError(f"download_mode must be wifi/ingest/defer/none, "
                             f"got {download_mode!r}")

        if self.use_timestamped_subdir:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self.session_dir = Path(self.root) / timestamp
        else:
            timestamp = time.strftime("%Y%m%d_%H%M%S")  # still used in manifest
            self.session_dir = Path(self.root)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.session_dir / "scan.log"

        def log(msg: str):
            line = f"[{time.strftime('%H:%M:%S')}] {msg}"
            print(line)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        log(f"=== ScanSession start: {n_shots} shots, dir={self.session_dir} ===")
        log(f"Trigger: {type(self.trigger).__name__}  download_mode={download_mode}")

        # 1. Verify connection + snapshot baseline filelist
        if not self.cam.connect():
            log("[ERR] Camera connect failed, aborting")
            return {"ok": False, "reason": "connect_failed"}
        log(f"Camera state={self.cam.params.get('state')}, "
            f"battery={self.cam.params.get('battery')}%, "
            f"firmware={self.cam.params.get('firmwareVersion')}")

        # Snapshot camera format mode (storages[0].format) at scan start.
        # We'll re-check at scan end to detect silent format flips like
        # RAW+JPG → RAW that K-S2 firmware sometimes does mid-session.
        format_at_start = None
        remain_at_start = None
        try:
            stors = self.cam.params.get("storages") or []
            if stors:
                format_at_start = stors[0].get("format")
                remain_at_start = stors[0].get("remain")
                log(f"Camera format: {format_at_start}  "
                    f"(remain={remain_at_start} shots)")
        except (IndexError, AttributeError, KeyError):
            pass

        # Record the mtime cutoff (fallback lower bound) BEFORE any slow camera
        # I/O. Primary frame detection is now per-shot stem capture from the
        # shoot loop, so this is only a safety net — but capturing it early
        # (before the baseline read / format probe) keeps it safely ahead of the
        # first frame even accounting for camera/laptop clock skew. Small slack.
        self.session_start_mtime = time.time() - 5.0

        # Baseline WiFi filelist: only needed for the post-scan diff used by
        # wifi/ingest (immediate-download) modes. In defer/none mode nothing
        # consumes before_files, and this call hits the slow, hang-prone
        # /v1/photos endpoint (30s timeout on this rig) — pure waste that also
        # delayed the mtime cutoff. Skip it entirely for defer/none.
        if download_mode in ("defer", "none"):
            self.before_files = set()
            log("Baseline filelist skipped (defer/none mode) — "
                "per-shot detection needs no card diff")
        else:
            self.before_files = self._snapshot_filelist()
            log(f"Baseline filelist (WiFi): {len(self.before_files)} "
                f"file(s) already on card")

        log(f"SD-card mtime cutoff (fallback): "
            f"{datetime.fromtimestamp(self.session_start_mtime).isoformat()}")

        # 2. Shoot loop
        successful = 0
        format_per_shot: List[Optional[str]] = []
        t_scan_start = time.time()
        for i in range(n_shots):
            if not self.trigger.wait(i, n_shots):
                log(f"[ERR] Trigger aborted at shot {i+1}/{n_shots}")
                break
            t0 = time.time()
            shutter_ok = self.cam.shoot(af=af, wait_idle=True)
            info = self.cam.get_latest_info() if shutter_ok else {}
            # A shot only COUNTS if the shutter released AND the camera actually
            # indexed a file. The shutter POST returning HTTP 200 is not proof a
            # frame was written: on an MF lens with af=auto the camera ACKs the
            # command but refuses to release (no focus lock), so /latest/info
            # never populates dir/file. Counting that as ok=True inflated
            # shots_captured and made ingest hunt for a frame that never existed.
            file_written = bool(info.get("dir") and info.get("file"))
            ok = shutter_ok and file_written
            if shutter_ok and not file_written:
                log(f"  [WARN!] shot {i+1}/{n_shots}: shutter ACKed but NO FILE "
                    f"indexed — treating as FAILED (not counted). "
                    f"Likely AF-priority refused to fire on an MF lens.")
            # Optional per-shot format snapshot (catches K-S2 firmware flips)
            shot_format = None
            if check_format_per_shot and ok:
                try:
                    fresh = self.cam.get_parameters()
                    stors = (fresh or {}).get("storages") or []
                    if stors:
                        shot_format = stors[0].get("format")
                except (IndexError, AttributeError, KeyError):
                    pass
            format_per_shot.append(shot_format)
            elapsed = time.time() - t0
            attempts = getattr(self.cam, "_info_attempts", None)
            entry = {
                "i": i + 1, "ok": ok,
                "dir": info.get("dir"), "file": info.get("file"),
                "elapsed_s": round(elapsed, 2),
                "info_attempts": attempts,
                "t": time.strftime("%H:%M:%S"),
                "format": shot_format,
            }
            self.shot_log.append(entry)
            warn = ""
            if elapsed > 10.0:
                warn = "  [WARN!] SLOW SHOT >10s (camera may have stalled)"
            elif elapsed > 5.0:
                warn = "  [WARN] slow shot >5s"
            fmt_str = f", fmt={shot_format}" if shot_format else ""
            log(f"  shot {i+1}/{n_shots}: ok={ok} {info.get('dir')}/{info.get('file')} "
                f"({elapsed:.2f}s, info_attempts={attempts}{fmt_str}){warn}")
            if ok:
                successful += 1
        t_scan_end = time.time()
        capture_dur = t_scan_end - t_scan_start
        per_shot = capture_dur / max(successful, 1)
        # Record session END time for a BOUNDED SD-card mtime window. Combined
        # with session_start_mtime, ingest can match only files written during
        # THIS scan (start <= mtime <= end), so test shots taken between scans
        # and frames from later scans no longer bleed into this scan's ingest.
        # Wall-clock only (no stat() calls) — fastest possible; +2s slack for
        # filesystem timestamp coarseness and any last-frame write lag.
        self.session_end_mtime = t_scan_end + 2.0
        log(f"=== Capture done: {successful}/{n_shots} ok in "
            f"{capture_dur:.1f}s ({per_shot:.2f}s/shot avg) ===")

        # Per-shot stats for stress-test analysis
        if self.shot_log:
            elapseds = [e["elapsed_s"] for e in self.shot_log if e["ok"]]
            attempts_list = [e["info_attempts"] for e in self.shot_log
                             if e.get("info_attempts") is not None]
            slow5 = sum(1 for e in elapseds if e > 5.0)
            slow10 = sum(1 for e in elapseds if e > 10.0)
            if elapseds:
                log(f"  shot time: min={min(elapseds):.2f}s "
                    f"max={max(elapseds):.2f}s "
                    f"avg={sum(elapseds)/len(elapseds):.2f}s")
            if attempts_list:
                log(f"  info_attempts: min={min(attempts_list)} "
                    f"max={max(attempts_list)} "
                    f"avg={sum(attempts_list)/len(attempts_list):.1f}")
            if slow5 or slow10:
                log(f"  [WARN] slow shots: {slow5} >5s, {slow10} >10s "
                    f"({100*slow5/len(elapseds):.0f}% / {100*slow10/len(elapseds):.0f}%)")

        # 3. Identify captured frames from the AUTHORITATIVE source: the per-shot
        # /latest/info responses already collected in self.shot_log during the
        # capture loop. Each successful shot recorded the camera's real dir/file,
        # so we know the exact filename of every frame — no need to diff the
        # (slow, hang-prone) full WiFi filelist, and no need to guess by mtime
        # windows at ingest. This is exact, fast, and immune to camera/laptop
        # clock skew. mtime bounds are still written to the manifest as a
        # last-ditch fallback, but stem-match is now the primary path.
        captured_files = [e["file"] for e in self.shot_log
                          if e.get("ok") and e.get("file")]
        new_filenames = set(captured_files)
        new_stems = {Path(n).stem for n in new_filenames}
        # new_paths kept for manifest/back-compat: dir/file joined where available.
        new_paths = [f"{e['dir']}/{e['file']}" for e in self.shot_log
                     if e.get("ok") and e.get("dir") and e.get("file")]
        log(f"Captured {len(new_stems)} frame(s) (from per-shot detection): "
            f"stems={sorted(new_stems)}")

        # Check format at scan end (compare against scan start to catch flips).
        format_at_end = None
        try:
            # Re-fetch params; cam.params is cache from last call so do a fresh
            # get_parameters() if available.
            fresh = self.cam.get_parameters() if hasattr(self.cam, "get_parameters") else self.cam.params
            stors = (fresh or {}).get("storages") or []
            if stors:
                format_at_end = stors[0].get("format")
        except (IndexError, AttributeError, KeyError):
            pass

        # Derive what format mode the scan actually produced, by counting how
        # many file extensions appear per stem in the new files. Used to give
        # honest counts in the ingest report.
        ext_by_stem: Dict[str, set] = {}
        for fn in new_filenames:
            p = Path(fn)
            ext_by_stem.setdefault(p.stem, set()).add(p.suffix.lower())
        # Format mode summary: e.g. "raw_only", "jpg_only", "raw+jpg", "mixed"
        if not ext_by_stem:
            observed_format_mode = "unknown"
        else:
            ext_sets = list(ext_by_stem.values())
            if all(s == {".dng"} for s in ext_sets) or all(s == {".pef"} for s in ext_sets):
                observed_format_mode = "raw_only"
            elif all(s == {".jpg"} or s == {".jpeg"} for s in ext_sets):
                observed_format_mode = "jpg_only"
            elif all(({".dng", ".jpg"} <= s) or ({".dng", ".jpeg"} <= s)
                     or ({".pef", ".jpg"} <= s) for s in ext_sets):
                observed_format_mode = "raw+jpg"
            else:
                observed_format_mode = "mixed"  # camera flipped mid-scan

        # Loud warning if format changed between start and end OR mid-scan
        if format_at_start and format_at_end and format_at_start != format_at_end:
            log(f"  [WARN!] FORMAT FLIP DETECTED: camera was '{format_at_start}' at "
                f"scan start, '{format_at_end}' at end")
            log(f"     This is a K-S2 firmware quirk. Verify the captured files")
            log(f"     before continuing the session.")
        if observed_format_mode == "mixed":
            log(f"  [WARN!] MIXED FORMAT MODE: not all shots have the same extensions")
            log(f"     stems with extensions: {ext_by_stem}")

        # Always persist a session manifest so any later `ingest` knows what
        # to look for. Includes both the stems (primary match key) and the
        # mtime cutoff (fallback if stems don't appear on disk).
        manifest_path = self.session_dir / "session.json"
        # Determine state — flipped to "ingested" later by either pyks2's own
        # ingest branch (immediate mode) or by the controller's deferred-ingest
        # routine.
        initial_state = ("ingested" if download_mode == "wifi"
                         else "pending_ingest" if download_mode in ("ingest", "defer")
                         else "no_ingest")
        manifest = {
            # --- Identity ---
            "session_id": timestamp,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ingested_at": None,
            "state": initial_state,
            "download_mode": download_mode,

            # --- Capture counts ---
            "shots_requested": n_shots,
            "shots_captured": successful,

            # --- New files detected on the SD card after capture ---
            "new_files_on_card_count": len(new_paths),
            "new_files_on_card_names": sorted(new_filenames),
            "new_files_on_card_stems": sorted(new_stems),
            "new_files_per_stem_extensions": {k: sorted(v) for k, v in ext_by_stem.items()},

            # --- Camera format state (catches RAW/RAW+JPG silent flips) ---
            "camera_format_at_start": format_at_start,
            "camera_format_at_end": format_at_end,
            "observed_format_mode": observed_format_mode,
            "per_shot_formats": format_per_shot if check_format_per_shot else None,

            # --- SD card state ---
            "sd_remain_shots_at_start": remain_at_start,
            "sd_mtime_cutoff": self.session_start_mtime,
            "sd_mtime_end": self.session_end_mtime,

            # --- Append history (populated by controller on append-to-scan) ---
            "appends": [],
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        log(f"Manifest written: {manifest_path}")

        # 4. Retrieve based on download_mode
        downloaded = 0
        ingest_summary: Dict[str, Any] = {}
        if download_mode == "wifi" and new_paths:
            log(f"Downloading {len(new_paths)} file(s) via WiFi to {self.session_dir}...")
            t_dl_start = time.time()
            for idx, path in enumerate(new_paths, 1):
                filename = path.split("/", 1)[-1]
                out = self.session_dir / filename
                t0 = time.time()
                ok = self.cam.download_image(path, str(out), size="full")
                dt = time.time() - t0
                log(f"  [{idx}/{len(new_paths)}] {path} -> {filename} "
                    f"({'OK' if ok else 'FAIL'}, {dt:.1f}s)")
                if ok:
                    downloaded += 1
            log(f"=== WiFi download done: {downloaded}/{len(new_paths)} "
                f"in {time.time()-t_dl_start:.1f}s ===")

        elif download_mode == "ingest" and successful > 0:
            log("")
            log(f"Capture complete. Plug in the SD card or USB MSC cable.")
            log(f"Expecting stems: {sorted(new_stems)}")
            log("")
            input("  Press Enter once the volume is mounted... ")
            ingest_summary = _ingest_into(
                self.session_dir, stems=new_stems,
                mtime_cutoff=self.session_start_mtime,
                expected_shots=successful, log_fn=log)
            downloaded = ingest_summary.get("copied", 0)
            # Flip state to ingested if ingest produced files
            if downloaded > 0:
                manifest["state"] = "ingested"
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, indent=2)

        elif download_mode == "defer":
            log("")
            log(f"download_mode=defer: capture complete, files remain on card.")
            log(f"Expecting stems: {sorted(new_stems)}")
            log(f"Run project-level ingest later to copy files.")

        elif download_mode == "none":
            log("download_mode=none: files remain on card. Run `ingest` later.")

        # 5. Summary
        summary = {
            "ok": True,
            "session_dir": str(self.session_dir),
            "requested": n_shots,
            "captured": successful,
            "new_on_card_wifi": len(new_paths),
            "downloaded": downloaded,
            "download_mode": download_mode,
            "log_path": str(log_path),
            "manifest": str(manifest_path),
        }
        if ingest_summary:
            summary["ingest"] = ingest_summary
        log(f"Summary: {summary}")
        self.trigger.close()
        return summary


def _ingest_into(session_dir: Path,
                 stems: Optional[set] = None,
                 mtime_cutoff: Optional[float] = None,
                 expected_shots: Optional[int] = None,
                 log_fn=print) -> Dict[str, Any]:
    """Shared ingest routine. Strategy:
      1. find live DCIM (rejects DCIM-bk and similar)
      2. PRIMARY: filename-stem match (handles RAW+JPG mode automatically)
      3. FALLBACK: mtime > cutoff, if stems found nothing
      4. SAFETY: if neither yields results, ask before copying anything
    """
    dcim = MediaIngest.find_dcim()
    if dcim is None:
        manual = input("  DCIM not auto-detected. Enter DCIM or volume path: ").strip()
        if manual:
            p = Path(manual)
            dcim = p if p.name.upper() == "DCIM" else p / "DCIM"
            if not dcim.exists():
                dcim = p
    if not dcim or not dcim.exists():
        log_fn("[ERR] Could not locate DCIM folder.")
        return {"copied": 0}

    chosen: List[Path] = []
    strategy = None

    # Try stem matching first
    if stems:
        chosen = MediaIngest.files_matching_stems(dcim, stems)
        if chosen:
            strategy = "stem-match"
            log_fn(f"  matched {len(chosen)} file(s) by stem "
                   f"(expecting {len(stems)} stems × 1-2 extensions)")

    # Fall back to mtime-newer-than
    if not chosen and mtime_cutoff is not None:
        chosen = MediaIngest.files_newer_than(dcim, mtime_cutoff)
        if chosen:
            strategy = "mtime"
            log_fn(f"  stem match found nothing — falling back to mtime cutoff: "
                   f"{len(chosen)} file(s) newer than "
                   f"{datetime.fromtimestamp(mtime_cutoff).strftime('%H:%M:%S')}")

    # Final fallback: ask the user
    if not chosen:
        all_files = MediaIngest.list_source_files(dcim)
        log_fn(f"  [WARN] Neither stems nor mtime found anything matching. "
               f"DCIM has {len(all_files)} photo file(s) total.")
        if expected_shots:
            log_fn(f"    Most recent {expected_shots*2} files might be your shots.")
            ans = input(f"  Copy newest {expected_shots*2} files? [y/N]: ").strip().lower()
            if ans == "y":
                chosen = MediaIngest.newest_n(dcim, expected_shots * 2)
                strategy = "newest-N-fallback"
        if not chosen:
            log_fn("  Aborted — no files copied.")
            return {"copied": 0}

    # Sanity check: warn if count is weird
    if expected_shots and strategy != "newest-N-fallback":
        ratio = len(chosen) / expected_shots
        if ratio in (1.0, 2.0) or abs(ratio - 1) < 0.05 or abs(ratio - 2) < 0.05:
            mode = "RAW+JPG" if ratio >= 1.5 else "single-format"
            log_fn(f"  [OK] {len(chosen)} file(s) for {expected_shots} shot(s) ({mode})")
        else:
            log_fn(f"  [WARN] {len(chosen)} files for {expected_shots} shots "
                   f"(ratio {ratio:.2f}) — expected 1× or 2×. Proceeding.")

    summary = MediaIngest.copy_files(chosen, session_dir)
    summary["strategy"] = strategy
    log_fn(f"=== Ingest done: {summary} ===")
    return summary


# ---------------------------------------------------------------------------
# CLI — focused on the debug workflow for this session.
# ---------------------------------------------------------------------------

HELP = """
Commands:
  c / connect    - test connection (GET /v1/props)
  s / state      - print current camera state
  w / wait       - poll until state=='idle'
  S / shoot      - fire shutter + wait for idle
  i / info       - GET /v1/photos/latest/info  (the workaround)
  l / latest     - download /v1/photos/latest?size=full -> ./latest.jpg
  L / list       - GET /v1/photos (the filelist)
  flow           - debug flow: shoot -> wait idle -> info -> download
  scan N [mode]  - run scan with N shots. mode = wifi|ingest|defer|none (default ingest)
                   wifi:   slow HTTP download (~60s/DNG)
                   ingest: capture only, then plug SD/USB to copy files
                   none:   capture only, ingest later via `ingest` command
  ingest [DIR]   - ingest files into a session dir (default: latest scan).
                   Uses session.json manifest to know which files to copy.
  q / quit
"""


def debug_flow(cam: "K_S2_WiFi"):
    """End-to-end test of the fix. Prints what works and what doesn't."""
    print("\n--- DEBUG FLOW ---")
    print("[1/5] connect")
    if not cam.connect():
        return
    print("[2/5] shoot + wait for idle")
    if not cam.shoot(af="auto", wait_idle=True):
        print("  shoot failed, aborting")
        return
    print("[3/5] get_latest_info  (the workaround)")
    info = cam.get_latest_info()
    if not info:
        print("  /latest/info returned nothing — falling back to filelist")
        cam.get_filelist()
    print("[4/5] download_latest")
    ok = cam.download_latest("./latest.jpg")
    print("[5/5] result:", "SUCCESS ✓" if ok else "FAILED ✗")


def main():
    print("Pentax K-S2 WiFi  (debug build)")
    ip = input("Camera IP [192.168.0.1]: ").strip() or "192.168.0.1"
    cam = K_S2_WiFi(ip)
    print(HELP)
    try:
        while True:
            cmd = input("> ").strip()
            if not cmd:
                continue
            c = cmd.lower()
            if c in ("q", "quit", "exit"):       break
            elif c in ("c", "connect"):           cam.connect()
            elif c in ("s", "state"):             print("state =", cam.get_state())
            elif c in ("w", "wait"):              cam.wait_for_idle()
            elif c in ("S", "shoot") or cmd == "S": cam.shoot()
            elif c in ("i", "info"):              cam.get_latest_info()
            elif c in ("l", "latest"):            cam.download_latest("./latest.jpg")
            elif c in ("L", "list") or cmd == "L": cam.get_filelist()
            elif c == "flow":                     debug_flow(cam)
            elif c.startswith("scan"):
                parts = cmd.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    print("usage: scan N [mode]   (mode = wifi|ingest|defer|none, default ingest)")
                    continue
                n = int(parts[1])
                mode = parts[2].lower() if len(parts) >= 3 else "ingest"
                if mode not in ("wifi", "ingest", "defer", "none"):
                    print(f"unknown mode {mode!r}, must be wifi|ingest|defer|none")
                    continue
                session = ScanSession(cam, trigger=ManualTrigger(), root="./scans")
                session.run(n_shots=n, download_mode=mode)
            elif c.startswith("ingest"):
                parts = cmd.split(maxsplit=1)
                if len(parts) == 2:
                    target = Path(parts[1])
                else:
                    scans_root = Path("./scans")
                    if not scans_root.exists():
                        print("no ./scans directory found")
                        continue
                    dirs = sorted([p for p in scans_root.iterdir() if p.is_dir()])
                    if not dirs:
                        print("no scan sessions found under ./scans")
                        continue
                    target = dirs[-1]
                    print(f"target session: {target}")
                if not target.exists():
                    print(f"  [ERR] target dir does not exist: {target}")
                    continue
                # Load session.json if present
                session_json = target / "session.json"
                stems = None
                mtime_cutoff = None
                expected_shots = None
                if session_json.exists():
                    with open(session_json, encoding="utf-8") as f:
                        meta = json.load(f)
                    # Read with backward compatibility: new field names first,
                    # fall back to old ones for migration-free reading.
                    stems = set(meta.get("new_files_on_card_stems") or
                                meta.get("wifi_stems") or [])
                    mtime_cutoff = (meta.get("sd_mtime_cutoff") or
                                    meta.get("mtime_cutoff"))
                    expected_shots = (meta.get("shots_captured") or
                                      meta.get("n_shots_captured"))
                    print(f"  Loaded manifest: {len(stems)} stems, "
                          f"{expected_shots} shots expected")
                else:
                    print(f"  [WARN] No session.json in {target}.")
                    ans = input(f"  Copy ALL photos from DCIM to {target}? [y/N]: ").strip().lower()
                    if ans != "y":
                        print("  Aborted.")
                        continue
                    # User opted in: pass nothing → final fallback will trigger
                _ingest_into(target, stems=stems, mtime_cutoff=mtime_cutoff,
                             expected_shots=expected_shots)
            elif c in ("h", "help", "?"):         print(HELP)
            else:
                print("unknown — try 'help'")
    except KeyboardInterrupt:
        print()
    finally:
        cam.close()


if __name__ == "__main__":
    main()
