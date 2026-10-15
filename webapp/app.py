import json
import os
import re
import subprocess
import tempfile
import uuid
import traceback
import time
from collections import Counter
from pathlib import Path
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Tuple

import imageio_ffmpeg
import requests
from flask import Flask, jsonify, redirect, render_template_string, request
from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload


SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/presentations",
]
APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
PACKAGE_DIR = PROJECT_ROOT / "output" / "gslides_package_all"
AUTH_STATE_FILE = APP_DIR / "broker_auth_state.json"
DRIVE_INDEX_FILE = APP_DIR / "drive_existing_media_index.json"
CAPTURE_CACHE_FILE = APP_DIR / "capture_time_cache.json"

DEFAULT_FOLDER_ID = ""
DEFAULT_AUTH_HUB_BASE = os.environ.get("AUTH_HUB_BASE", "http://127.0.0.1:8790").rstrip("/")
DEFAULT_TARGET_APP = os.environ.get("AUTH_TARGET_APP", "presentationPublisher")
DEFAULT_BROKER_BASE = os.environ.get(
    "BROKER_BASE", "https://your-auth-broker.example.com"
).rstrip("/")
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "8788"))
DEFAULT_CHUNK_SIZE_MB = int(os.environ.get("CHUNK_SIZE_MB", "90"))
DEFAULT_EXISTING_PRESENTATION_ID = os.environ.get(
    "EXISTING_PRESENTATION_ID", ""
).strip()
MAX_LOG_LINES = 400
RUNTIME_LOGS: List[str] = []
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
SUPPORTED_VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
BASE_SOURCE_FOLDERS = ("Sample", "Timo", "AlbumC")
PRIMARY_FOLDER_NAME = "Highlights"
DEBUG_LOG_PATH = PROJECT_ROOT / "debug-98df17.log"
REQUEST_TIMEOUT_SEC = int(os.environ.get("REQUEST_TIMEOUT_SEC", "120"))
MAX_RETRIES_PER_ITEM = int(os.environ.get("MAX_RETRIES_PER_ITEM", "3"))
BATCH_REQUEST_SIZE = int(os.environ.get("BATCH_REQUEST_SIZE", "30"))
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "5"))

HTML = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Slides Uploader</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 900px; }
    input, button { padding: 10px; font-size: 14px; }
    .row { margin-bottom: 12px; }
    pre { background:#f5f5f5; padding: 12px; border-radius: 8px; overflow:auto; }
  </style>
</head>
<body>
  <h2>Mini App: Drive Upload + Slides Build</h2>
  {% if login_ok %}
  <div style="background:#e8f5e9;border:1px solid #81c784;padding:10px;border-radius:8px;margin-bottom:12px;">
    Login erfolgreich. Du kannst jetzt direkt veröffentlichen oder über "Apps anzeigen" zu den angebundenen Apps zurückspringen.
  </div>
  {% endif %}
  <div class="row">
    <label>Broker Base:</label><br>
    <input id="brokerBase" style="width:520px" value="{{broker_base}}" />
  </div>
  <div class="row">
    <label>Target App:</label><br>
    <input id="targetApp" style="width:520px" value="{{target_app}}" />
  </div>
  <div class="row">
    <button onclick="listApps()">Apps anzeigen</button>
    <button onclick="startAuth()">Mit Auth-Hub verbinden</button>
    <button onclick="authStatus()">Auth-Status prüfen</button>
  </div>
  <div class="row">
    <label>Drive Folder ID:</label><br>
    <input id="folderId" style="width:520px" value="{{folder_id}}" />
  </div>
  <div class="row">
    <label>Chunk Size (MB):</label><br>
    <input id="chunkSizeMb" style="width:220px" value="{{chunk_size_mb}}" />
  </div>
  <div class="row">
    <button onclick="runAll()">Upload + Präsentation erstellen</button>
    <button onclick="runMini()">Mini Präsentation testen</button>
  </div>
  <pre id="out">Bereit.</pre>
  <h3>Live-Log</h3>
  <pre id="logbox" style="height:260px; overflow-y:auto;">Warte auf Logdaten...</pre>
  <script>
    async function startAuth() {
      const out = document.getElementById('out');
      const brokerBase = document.getElementById('brokerBase').value.trim();
      const targetApp = document.getElementById('targetApp').value.trim();
      const r = await fetch('/auth/start?brokerBase=' + encodeURIComponent(brokerBase) + '&targetApp=' + encodeURIComponent(targetApp));
      const data = await r.json();
      if (data.ok && data.url) {
        window.open(data.url, '_blank');
        out.textContent = 'OAuth-Tab geöffnet. Nach erfolgreichem Login zur App zurückkehren und Auth-Status prüfen.';
      } else {
        out.textContent = JSON.stringify(data, null, 2);
      }
    }
    async function listApps() {
      const out = document.getElementById('out');
      const brokerBase = document.getElementById('brokerBase').value.trim();
      const r = await fetch('/auth/apps?brokerBase=' + encodeURIComponent(brokerBase));
      const data = await r.json();
      out.textContent = JSON.stringify(data, null, 2);
    }
    async function authStatus() {
      const out = document.getElementById('out');
      const brokerBase = document.getElementById('brokerBase').value.trim();
      const targetApp = document.getElementById('targetApp').value.trim();
      const r = await fetch('/auth/status?brokerBase=' + encodeURIComponent(brokerBase) + '&targetApp=' + encodeURIComponent(targetApp));
      const data = await r.json();
      out.textContent = JSON.stringify(data, null, 2);
    }
    async function runAll() {
      const out = document.getElementById('out');
      out.textContent = 'Läuft...';
      const folderId = document.getElementById('folderId').value.trim();
      const brokerBase = document.getElementById('brokerBase').value.trim();
      const targetApp = document.getElementById('targetApp').value.trim();
      const chunkSizeMb = Number(document.getElementById('chunkSizeMb').value || '90');
      const r = await fetch('/run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({folderId, targetApp, brokerBase, chunkSizeMb})
      });
      const data = await r.json();
      out.textContent = JSON.stringify(data, null, 2);
    }
    async function runMini() {
      const out = document.getElementById('out');
      out.textContent = 'Mini-Test läuft...';
      const folderId = document.getElementById('folderId').value.trim();
      const brokerBase = document.getElementById('brokerBase').value.trim();
      const targetApp = document.getElementById('targetApp').value.trim();
      const r = await fetch('/run-mini', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({folderId, targetApp, brokerBase})
      });
      const data = await r.json();
      out.textContent = JSON.stringify(data, null, 2);
    }
    async function refreshLogs() {
      try {
        const r = await fetch('/logs');
        const data = await r.json();
        const box = document.getElementById('logbox');
        box.textContent = (data.logs || []).join('\\n');
        box.scrollTop = box.scrollHeight;
      } catch (e) {
      }
    }
    setInterval(refreshLogs, 1500);
    refreshLogs();
  </script>
</body>
</html>
"""


def auth_hub_apps(hub_base: str) -> dict:
    res = requests.get(f"{hub_base}/apps", timeout=30)
    res.raise_for_status()
    return res.json()


def log_event(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    RUNTIME_LOGS.append(f"[{timestamp}] {message}")
    if len(RUNTIME_LOGS) > MAX_LOG_LINES:
        del RUNTIME_LOGS[:-MAX_LOG_LINES]


# #region agent log
def debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    try:
        payload = {
            "sessionId": "98df17",
            "runId": run_id,
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(datetime.utcnow().timestamp() * 1000),
        }
        with DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


# #endregion


def utc_iso_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def new_run_log(mode: str, target_app: str, target_folder_id: str) -> dict:
    return {
        "runId": str(uuid.uuid4()),
        "mode": mode,
        "targetApp": target_app,
        "targetFolderId": target_folder_id,
        "startedAt": utc_iso_now(),
        "finishedAt": None,
        "status": "running",
        "steps": [],
        "completedItems": [],
        "openIssues": [],
        "nextActions": [],
        "publishTargets": [],
        "orderedMergeInstructions": [],
        "confirmedHypothesisCandidates": [],
        "rejectedHypothesisCandidates": [],
        "inconclusiveHypothesisCandidates": [],
        "nextDiagnosticActions": [],
    }


def run_log_step(run_log: dict, step: str, status: str, detail: str) -> None:
    run_log.setdefault("steps", []).append(
        {"at": utc_iso_now(), "step": step, "status": status, "detail": detail}
    )


def run_log_complete(run_log: dict, ok: bool, error: str = "") -> None:
    run_log["finishedAt"] = utc_iso_now()
    run_log["status"] = "ok" if ok else "error"
    if error:
        run_log.setdefault("openIssues", []).append(error)


def build_drive_client(access_token: str):
    creds = Credentials(token=access_token, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def write_json_file_to_drive(drive, parent_id: str, file_name: str, payload: dict) -> str:
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    media = MediaIoBaseUpload(BytesIO(data), mimetype="application/json", resumable=False)
    created = drive.files().create(
        body={"name": file_name, "parents": [parent_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    file_id = created.get("id")
    if not file_id:
        raise RuntimeError(f"Konnte Drive-Logdatei nicht speichern: {file_name}")
    return file_id


def upsert_latest_status_file(drive, logs_folder_id: str, payload: dict) -> str:
    query = (
        f"'{logs_folder_id}' in parents and trashed=false and "
        "name='latest_status.json' and mimeType='application/json'"
    )
    files = (
        drive.files()
        .list(
            q=query,
            fields="files(id,name)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    media = MediaIoBaseUpload(BytesIO(data), mimetype="application/json", resumable=False)
    if files:
        file_id = files[0]["id"]
        drive.files().update(
            fileId=file_id,
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        return file_id
    created = drive.files().create(
        body={"name": "latest_status.json", "parents": [logs_folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return str(created.get("id") or "")


def persist_run_log_to_drive(access_token: str, target_folder_id: str, run_log: dict) -> None:
    drive = build_drive_client(access_token)
    # #region agent log
    debug_log(run_log.get("runId", "runtime"), "H5", "webapp/app.py:persist_run_log_to_drive", "Persist run log start", {"targetFolderId": target_folder_id, "status": run_log.get("status")})
    # #endregion
    logs_folder_id = drive_find_or_create_subfolder(drive, target_folder_id, "Logs")
    file_name = f"run_{run_log['startedAt'].replace(':', '').replace('-', '')}_{run_log['runId'][:8]}.json"
    run_file_id = write_json_file_to_drive(drive, logs_folder_id, file_name, run_log)
    latest_id = upsert_latest_status_file(drive, logs_folder_id, run_log)
    run_log["driveLogFileId"] = run_file_id
    run_log["driveLatestStatusFileId"] = latest_id
    log_event(f"Drive-Log gespeichert in 'Logs': {file_name}")
    # #region agent log
    debug_log(run_log.get("runId", "runtime"), "H5", "webapp/app.py:persist_run_log_to_drive", "Persist run log ok", {"logsFolderIdSuffix": logs_folder_id[-8:] if logs_folder_id else "", "runLogFileIdSuffix": run_file_id[-8:] if run_file_id else ""})
    # #endregion


def state_file_path(run_id: str) -> Path:
    return APP_DIR / f"run_state_{run_id}.json"


def save_run_state(run_state: dict) -> None:
    path = state_file_path(run_state["runId"])
    path.write_text(json.dumps(run_state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_resume_run_state(target_folder_id: str) -> dict:
    candidates = sorted(APP_DIR.glob("run_state_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("targetFolderId") != target_folder_id:
            continue
        if payload.get("status") != "interrupted":
            continue
        return payload
    return {}


def is_transient_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    needles = ["ssl", "eof", "connection reset", "connection aborted", "timed out", "timeout", "protocol"]
    return any(n in text for n in needles)


def retry_with_backoff(fn, *, retries: int, run_id: str, phase: str, item_index: int):
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            transient = is_transient_error(exc)
            # #region agent log
            debug_log(
                run_id,
                "H1",
                "webapp/app.py:retry_with_backoff",
                "Retryable operation failed",
                {"phase": phase, "itemIndex": item_index, "attempt": attempt + 1, "transient": transient, "errorType": type(exc).__name__, "error": str(exc)[:280]},
            )
            # #endregion
            if (not transient) or attempt >= retries:
                raise
            sleep_s = 2 ** attempt
            time.sleep(sleep_s)
            attempt += 1


def update_run_state_and_maybe_persist(run_state: dict, access_token: str, target_folder_id: str, run_log: dict, *, force: bool = False) -> None:
    save_run_state(run_state)
    interval = max(1, CHECKPOINT_INTERVAL)
    if force or (run_state.get("lastCompletedItemIndex", -1) + 1) % interval == 0:
        run_log["checkpoint"] = {
            "lastCompletedItemIndex": run_state.get("lastCompletedItemIndex", -1),
            "nextSlideNo": run_state.get("nextSlideNo", 0),
            "completedCount": len(run_state.get("completedFileIds", [])),
        }
        persist_run_log_to_drive(access_token, target_folder_id, run_log)


def auth_hub_oauth_url(hub_base: str, target_app: str, return_to: str) -> str:
    params = {"targetApp": target_app}
    if return_to:
        params["returnTo"] = return_to
    res = requests.get(f"{hub_base}/oauth/start", params=params, timeout=30)
    payload = {}
    try:
        payload = res.json()
    except Exception:
        pass
    if res.status_code >= 400:
        raise RuntimeError(payload.get("error") if payload else f"Auth Hub Fehler ({res.status_code})")
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", "Auth Hub lieferte keine OAuth URL."))
    url = payload.get("url")
    if not url:
        raise RuntimeError("Auth Hub lieferte keine OAuth URL.")
    return url


def auth_hub_access_token(hub_base: str, target_app: str) -> str:
    state = load_auth_state()
    session_id = state.get("oauthSessionId")
    broker_token = state.get("brokerToken")
    if not session_id and not broker_token:
        raise RuntimeError("Keine OAuth-Session vorhanden. Bitte zuerst verbinden.")
    params = {"targetApp": target_app}
    if session_id:
        params["sessionId"] = session_id
    if broker_token:
        params["brokerToken"] = broker_token
    res = requests.get(f"{hub_base}/token", params=params, timeout=30)
    payload = {}
    try:
        payload = res.json()
    except Exception:
        pass
    if res.status_code >= 400:
        raise RuntimeError(payload.get("error") if payload else f"Auth Hub Tokenfehler ({res.status_code})")
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(payload.get("error", "Auth Hub lieferte kein access_token."))
    return token


def load_auth_state() -> dict:
    if AUTH_STATE_FILE.exists():
        try:
            return json.loads(AUTH_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_auth_state(state: dict) -> None:
    AUTH_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def drive_find_or_create_subfolder(drive, parent_id: str, name: str) -> str:
    escaped_name = name.replace("'", "\\'")
    q = (
        f"'{parent_id}' in parents and "
        f"name = '{escaped_name}' and "
        "mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res = drive.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    created = drive.files().create(body=body, fields="id").execute()
    return created["id"]


def drive_upload_file(drive, local_path: Path, parent_id: str, mime_type: str = None) -> str:
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
    body = {"name": local_path.name, "parents": [parent_id]}
    created = drive.files().create(body=body, media_body=media, fields="id").execute()
    return created["id"]


def upload_package_to_drive(drive, package_dir: Path, target_folder_id: str) -> Dict[str, str]:
    if not package_dir.exists():
        raise RuntimeError(f"Paketordner nicht gefunden: {package_dir}")
    manifest_path = package_dir / "manifest.json"
    media_dir = package_dir / "media"
    if not manifest_path.exists() or not media_dir.exists():
        raise RuntimeError("manifest.json oder media/ fehlt im Paket.")

    media_folder_id = drive_find_or_create_subfolder(drive, target_folder_id, "media")
    uploaded: Dict[str, str] = {}

    manifest_id = drive_upload_file(drive, manifest_path, target_folder_id, "application/json")
    uploaded["manifest.json"] = manifest_id

    for p in sorted(media_dir.glob("*")):
        if p.is_file():
            file_id = drive_upload_file(drive, p, media_folder_id)
            uploaded[f"media/{p.name}"] = file_id
    return uploaded


def load_manifest(package_dir: Path) -> dict:
    return json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))


def broker_publish_slides(
    broker_base: str,
    access_token: str,
    folder_id: str,
    package_dir: Path,
    manifest: dict,
) -> dict:
    log_event("Broker-Publish gestartet: Manifest wird gesendet.")
    files_payload = []
    opened = []
    try:
        for entry in manifest.get("entries", []):
            rel = entry.get("file")
            if not rel:
                continue
            p = package_dir / rel
            if not p.exists():
                continue
            fh = open(p, "rb")
            opened.append(fh)
            files_payload.append((f"file:{rel}", (p.name, fh, None)))

        data = {
            "accessToken": access_token,
            "folderId": folder_id,
            "manifest": json.dumps(manifest, ensure_ascii=False),
        }
        response = requests.post(
            f"{broker_base}/api/publish/slides-from-manifest",
            data=data,
            files=files_payload,
            timeout=1200,
        )
        log_event(f"Broker antwortete mit Status {response.status_code}.")
        payload = {}
        try:
            payload = response.json()
        except Exception:
            payload = {"error": response.text[:1200]}
        if response.status_code >= 400:
            log_event("Broker-Publish fehlgeschlagen.")
            raise RuntimeError(payload.get("error", f"Broker publish failed ({response.status_code})"))
        log_event("Broker-Publish erfolgreich abgeschlossen.")
        return payload
    finally:
        for fh in opened:
            try:
                fh.close()
            except Exception:
                pass


def build_slides_from_manifest(slides, manifest: dict, uploaded_map: Dict[str, str]) -> Tuple[str, int]:
    log_event("Lokaler Fallback: Google Slides Präsentation wird erstellt.")
    pres = slides.presentations().create(body={"title": "Auto Generated Media Deck"}).execute()
    pres_id = pres["presentationId"]
    first_slide_id = pres["slides"][0]["objectId"]

    requests: List[dict] = [{"deleteObject": {"objectId": first_slide_id}}]
    page_w = 960
    page_h = 540

    for idx, entry in enumerate(manifest["entries"], start=1):
        slide_id = f"slide_{idx:04d}"
        requests.append({"createSlide": {"objectId": slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})

        entry_type = entry["type"]
        rel = entry["file"]
        file_id = uploaded_map.get(rel)
        if not file_id:
            continue

        if entry_type in ("image", "video_poster"):
            requests.append(
                {
                    "createImage": {
                        "objectId": f"img_{idx:04d}",
                        "url": f"https://drive.google.com/uc?id={file_id}",
                        "elementProperties": {
                            "pageObjectId": slide_id,
                            "size": {
                                "width": {"magnitude": page_w, "unit": "PT"},
                                "height": {"magnitude": page_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"},
                        },
                    }
                }
            )
        elif entry_type == "video":
            video_id = f"vid_{idx:04d}"
            requests.append(
                {
                    "createVideo": {
                        "objectId": video_id,
                        "source": "DRIVE",
                        "id": file_id,
                        "elementProperties": {
                            "pageObjectId": slide_id,
                            "size": {
                                "width": {"magnitude": page_w, "unit": "PT"},
                                "height": {"magnitude": page_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"},
                        },
                    }
                }
            )
            if entry.get("autoplayRequested", False):
                requests.append(
                    {
                        "updateVideoProperties": {
                            "objectId": video_id,
                            "videoProperties": {"autoPlay": True},
                            "fields": "autoPlay",
                        }
                    }
                )

    slides.presentations().batchUpdate(presentationId=pres_id, body={"requests": requests}).execute()
    log_event(f"Lokaler Fallback abgeschlossen: {len(manifest['entries'])} Slides erstellt.")
    return pres_id, len(manifest["entries"])


def build_slides_from_drive_index(slides, indexed_items: List[dict]) -> Tuple[str, int]:
    log_event("Erstelle Präsentation direkt aus vorhandenen Drive-Dateien (ohne Upload).")
    pres = slides.presentations().create(body={"title": "Auto Generated Existing Media Deck"}).execute()
    pres_id = pres["presentationId"]
    first_slide_id = pres["slides"][0]["objectId"]
    requests: List[dict] = [{"deleteObject": {"objectId": first_slide_id}}]
    page_w = 960
    page_h = 540

    for idx, item in enumerate(indexed_items, start=1):
        slide_id = f"slide_{idx:04d}"
        requests.append({"createSlide": {"objectId": slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}})
        mime_type = str(item.get("mimeType", ""))
        drive_id = str(item.get("id", ""))
        if not drive_id:
            continue
        if mime_type.startswith("image/"):
            requests.append(
                {
                    "createImage": {
                        "objectId": f"img_{idx:04d}",
                        "url": f"https://drive.google.com/uc?id={drive_id}",
                        "elementProperties": {
                            "pageObjectId": slide_id,
                            "size": {
                                "width": {"magnitude": page_w, "unit": "PT"},
                                "height": {"magnitude": page_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"},
                        },
                    }
                }
            )
        elif mime_type.startswith("video/"):
            requests.append(
                {
                    "createVideo": {
                        "objectId": f"vid_{idx:04d}",
                        "source": "DRIVE",
                        "id": drive_id,
                        "elementProperties": {
                            "pageObjectId": slide_id,
                            "size": {
                                "width": {"magnitude": page_w, "unit": "PT"},
                                "height": {"magnitude": page_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"},
                        },
                    }
                }
            )

    slides.presentations().batchUpdate(presentationId=pres_id, body={"requests": requests}).execute()
    log_event(f"Präsentation aus vorhandenen Drive-Dateien erstellt: {len(indexed_items)} Slides.")
    return pres_id, len(indexed_items)


def compute_contain_box(media_w: int, media_h: int, page_w: int = 960, page_h: int = 540) -> Tuple[float, float, float, float]:
    if media_w <= 0 or media_h <= 0:
        return 0.0, 0.0, float(page_w), float(page_h)
    scale = min(page_w / media_w, page_h / media_h)
    draw_w = media_w * scale
    draw_h = media_h * scale
    left = (page_w - draw_w) / 2
    top = (page_h - draw_h) / 2
    return left, top, draw_w, draw_h


def dimension_to_pt(dim: dict, fallback: float) -> float:
    if not isinstance(dim, dict):
        return fallback
    magnitude = dim.get("magnitude")
    unit = str(dim.get("unit") or "PT").upper()
    try:
        mag = float(magnitude)
    except Exception:
        return fallback
    if unit == "PT":
        return mag
    # 1 pt = 12700 EMU
    if unit == "EMU":
        return mag / 12700.0
    # Fallback: treat as pt for unknown units.
    return mag


def fetch_presentation_page_size_pt(slides, presentation_id: str) -> Tuple[float, float]:
    meta = slides.presentations().get(presentationId=presentation_id, fields="pageSize").execute()
    page_size = meta.get("pageSize") or {}
    width_pt = dimension_to_pt(page_size.get("width"), 960.0)
    height_pt = dimension_to_pt(page_size.get("height"), 540.0)
    # Guard against invalid or zero values.
    if width_pt <= 0 or height_pt <= 0:
        return 960.0, 540.0
    return width_pt, height_pt


def prepare_existing_presentation_for_rebuild(slides, presentation_id: str, run_id: str) -> str:
    meta = slides.presentations().get(
        presentationId=presentation_id,
        fields="slides(objectId,pageElements(objectId))",
    ).execute()
    slides_list = meta.get("slides", []) or []
    if not slides_list:
        created = slides.presentations().batchUpdate(
            presentationId=presentation_id,
            body={"requests": [{"createSlide": {"objectId": "slide_0001", "slideLayoutReference": {"predefinedLayout": "BLANK"}}}]},
        ).execute()
        _ = created
        return "slide_0001"

    first_slide = slides_list[0]
    first_slide_id = first_slide.get("objectId", "")
    requests_batch: List[dict] = []
    for page_el in first_slide.get("pageElements", []) or []:
        obj_id = page_el.get("objectId")
        if obj_id:
            requests_batch.append({"deleteObject": {"objectId": obj_id}})
    for slide in slides_list[1:]:
        sid = slide.get("objectId")
        if sid:
            requests_batch.append({"deleteObject": {"objectId": sid}})
    if requests_batch:
        retry_with_backoff(
            lambda: slides.presentations().batchUpdate(
                presentationId=presentation_id,
                body={"requests": requests_batch},
            ).execute(),
            retries=MAX_RETRIES_PER_ITEM,
            run_id=run_id,
            phase="slides_prepare_rebuild",
            item_index=-1,
        )
    return first_slide_id


def render_professional_background(image_bytes: bytes, canvas_w: int = 1920, canvas_h: int = 1080) -> bytes:
    with Image.open(BytesIO(image_bytes)) as src_img:
        src = ImageOps.exif_transpose(src_img.convert("RGB"))
        bg = ImageOps.fit(src, (canvas_w, canvas_h), method=Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=42))
        bg = ImageEnhance.Brightness(bg).enhance(0.78)
        out = BytesIO()
        bg.save(out, format="JPEG", quality=90)
        return out.getvalue()


def upload_generated_background(drive, parent_id: str, name: str, jpg_bytes: bytes) -> str:
    media = MediaIoBaseUpload(BytesIO(jpg_bytes), mimetype="image/jpeg", resumable=False)
    created = drive.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()
    file_id = created.get("id")
    if not file_id:
        raise RuntimeError("Konnte erzeugten Hintergrund nicht hochladen.")
    drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
        sendNotificationEmail=False,
    ).execute()
    return file_id


def ensure_blurry_background_folder(drive, parent_id: str) -> str:
    escaped = "Blurry Background Images".replace("'", "\\'")
    query = (
        f"'{parent_id}' in parents and trashed=false and "
        f"mimeType='application/vnd.google-apps.folder' and name='{escaped}'"
    )
    existing = (
        drive.files()
        .list(
            q=query,
            fields="files(id,name)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    if existing:
        return existing[0]["id"]
    created = (
        drive.files()
        .create(
            body={
                "name": "Blurry Background Images",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    folder_id = created.get("id")
    if not folder_id:
        raise RuntimeError("Konnte Ordner 'Blurry Background Images' nicht erstellen.")
    return folder_id


def find_background_file_id(drive, parent_id: str, name: str) -> str:
    escaped = name.replace("'", "\\'")
    query = (
        f"'{parent_id}' in parents and trashed=false and "
        f"name='{escaped}' and mimeType='image/jpeg'"
    )
    files = (
        drive.files()
        .list(
            q=query,
            fields="files(id,name)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    return files[0]["id"] if files else ""


def get_media_dimensions_and_thumb(drive, file_id: str) -> Tuple[int, int, str]:
    meta = drive.files().get(
        fileId=file_id,
        fields="mimeType,imageMediaMetadata(width,height),videoMediaMetadata(width,height),thumbnailLink",
        supportsAllDrives=True,
    ).execute()
    mime = str(meta.get("mimeType", ""))
    if mime.startswith("image/"):
        im = meta.get("imageMediaMetadata") or {}
        return int(im.get("width") or 0), int(im.get("height") or 0), str(meta.get("thumbnailLink") or "")
    vm = meta.get("videoMediaMetadata") or {}
    return int(vm.get("width") or 0), int(vm.get("height") or 0), str(meta.get("thumbnailLink") or "")


def download_drive_file_bytes(access_token: str, file_id: str) -> bytes:
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    # #region agent log
    debug_log("runtime", "H1", "webapp/app.py:download_drive_file_bytes", "Drive download start", {"fileIdSuffix": file_id[-8:] if file_id else ""})
    # #endregion
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=120)
        r.raise_for_status()
        # #region agent log
        debug_log("runtime", "H1", "webapp/app.py:download_drive_file_bytes", "Drive download ok", {"fileIdSuffix": file_id[-8:] if file_id else "", "bytes": len(r.content)})
        # #endregion
        return r.content
    except Exception as exc:
        # #region agent log
        debug_log("runtime", "H1", "webapp/app.py:download_drive_file_bytes", "Drive download failed", {"fileIdSuffix": file_id[-8:] if file_id else "", "errorType": type(exc).__name__, "error": str(exc)[:250]})
        # #endregion
        raise


def download_video_thumbnail(access_token: str, thumbnail_link: str) -> bytes:
    if not thumbnail_link:
        return b""
    r = requests.get(thumbnail_link, headers={"Authorization": f"Bearer {access_token}"}, timeout=60)
    if r.status_code >= 400:
        return b""
    return r.content


def parse_exif_datetime(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_image_capture_time(image_bytes: bytes) -> datetime | None:
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            exif = img.getexif()
            if not exif:
                return None
            for tag in (36867, 36868, 306):
                raw = exif.get(tag)
                if not raw:
                    continue
                parsed = parse_exif_datetime(str(raw))
                if parsed:
                    return parsed
    except Exception:
        return None
    return None


def parse_video_capture_time(video_bytes: bytes) -> datetime | None:
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="video-capture-time-") as tmp:
        video_path = Path(tmp) / "input_video.mp4"
        video_path.write_bytes(video_bytes)
        proc = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(video_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            encoding="utf-8",
            errors="ignore",
        )
        raw = proc.stderr or proc.stdout or ""
        match = re.search(r"creation_time\s*:\s*([^\n\r]+)", raw)
        if not match:
            return None
        value = match.group(1).strip().replace("Z", "+00:00")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                if dt.tzinfo:
                    return dt.replace(tzinfo=None)
                return dt
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo:
                return dt.replace(tzinfo=None)
            return dt
        except Exception:
            return None


def load_capture_cache() -> dict:
    if not CAPTURE_CACHE_FILE.exists():
        return {}
    try:
        payload = json.loads(CAPTURE_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except Exception:
        return {}
    return {}


def save_capture_cache(payload: dict) -> None:
    CAPTURE_CACHE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_drive_image_time(raw_value) -> datetime | None:
    if raw_value is None:
        return None
    try:
        val = float(str(raw_value))
        # Drive imageMediaMetadata.time is typically epoch milliseconds.
        if val > 10_000_000_000:
            return datetime.utcfromtimestamp(val / 1000.0)
        return datetime.utcfromtimestamp(val)
    except Exception:
        return None


def get_drive_capture_metadata(drive, file_id: str) -> dict:
    meta = drive.files().get(
        fileId=file_id,
        fields="createdTime,imageMediaMetadata(time),videoMediaMetadata",
        supportsAllDrives=True,
    ).execute()
    return {
        "createdTime": str(meta.get("createdTime") or ""),
        "imageTime": (meta.get("imageMediaMetadata") or {}).get("time"),
    }


def enrich_and_sort_items_by_capture_time(items: List[dict], access_token: str, run_id: str, drive) -> List[dict]:
    cache = load_capture_cache()
    changed = False
    enriched_items: List[dict] = []
    for idx, item in enumerate(items):
        file_id = str(item.get("id", ""))
        if not file_id:
            continue
        mime = str(item.get("mimeType", ""))
        fingerprint = f"{item.get('modifiedTime','')}|{item.get('size','')}|{mime}"
        cache_entry = cache.get(file_id, {})
        capture_iso = str(cache_entry.get("captureTime") or "")
        source = str(cache_entry.get("source") or "")
        needs_refresh = (
            str(cache_entry.get("fingerprint") or "") != fingerprint
            or not capture_iso
            or source in ("drive_createdTime_fallback", "unknown")
        )
        if needs_refresh:
            capture_dt = None
            source = "drive_createdTime_fallback"
            drive_meta: dict = {}
            try:
                drive_meta = retry_with_backoff(
                    lambda: get_drive_capture_metadata(drive, file_id),
                    retries=MAX_RETRIES_PER_ITEM,
                    run_id=run_id,
                    phase="drive_get_metadata",
                    item_index=idx,
                )
                if mime.startswith("image/"):
                    capture_dt = parse_drive_image_time(drive_meta.get("imageTime"))
                    if capture_dt:
                        source = "drive_image_metadata_time"
                if mime.startswith("image/") and not capture_dt:
                    image_bytes = retry_with_backoff(
                        lambda: download_drive_file_bytes(access_token, file_id),
                        retries=MAX_RETRIES_PER_ITEM,
                        run_id=run_id,
                        phase="download_source",
                        item_index=idx,
                    )
                    capture_dt = parse_image_capture_time(image_bytes)
                    if capture_dt:
                        source = "image_exif"
                elif mime.startswith("video/"):
                    video_bytes = retry_with_backoff(
                        lambda: download_drive_file_bytes(access_token, file_id),
                        retries=MAX_RETRIES_PER_ITEM,
                        run_id=run_id,
                        phase="download_source",
                        item_index=idx,
                    )
                    capture_dt = parse_video_capture_time(video_bytes)
                    if capture_dt:
                        source = "video_creation_time"
            except Exception:
                capture_dt = None
            if not capture_dt:
                raw = str((drive_meta or {}).get("createdTime") or item.get("createdTime") or "")
                try:
                    capture_dt = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    capture_dt = datetime.utcnow()
            capture_iso = capture_dt.isoformat(timespec="seconds")
            cache[file_id] = {"fingerprint": fingerprint, "captureTime": capture_iso, "source": source}
            changed = True
        updated = dict(item)
        updated["captureTime"] = capture_iso
        updated["captureTimeSource"] = source or "unknown"
        enriched_items.append(updated)
    if changed:
        save_capture_cache(cache)
    enriched_items.sort(
        key=lambda x: (
            x.get("captureTime") or "",
            x.get("name") or "",
            x.get("id") or "",
        )
    )
    return enriched_items


def sharpness_score(image_bytes: bytes) -> float:
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            rgb = ImageOps.exif_transpose(img.convert("RGB"))
            edges = rgb.filter(ImageFilter.FIND_EDGES)
            stat = ImageStat.Stat(edges)
            # Mean variance across channels as simple sharpness proxy.
            return float(sum(stat.var) / max(1, len(stat.var)))
    except Exception:
        return 0.0


def extract_first_video_frame(video_bytes: bytes) -> bytes:
    ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="mini-video-frame-") as tmp:
        tmp_path = Path(tmp)
        video_path = tmp_path / "input_video.mp4"
        video_path.write_bytes(video_bytes)
        frame_path = tmp_path / "frame_001.jpg"
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(frame_path),
        ]
        subprocess.run(cmd, check=False)
        if not frame_path.exists():
            return b""
        return frame_path.read_bytes()


def build_styled_mini_presentation(
    drive,
    slides,
    access_token: str,
    folder_id: str,
    items: List[dict],
    *,
    run_id: str = "runtime",
    run_state: dict | None = None,
    run_log: dict | None = None,
) -> Tuple[str, int]:
    bg_folder_id = ensure_blurry_background_folder(drive, folder_id)
    log_event("Nutze Ordner 'Blurry Background Images' für Hintergrundbilder.")
    existing_pres_id = str((run_state or {}).get("presentationId") or "")
    force_rebuild = bool((run_state or {}).get("forceRebuild"))
    reusable_first_slide_id = ""
    if existing_pres_id:
        pres_id = existing_pres_id
        if force_rebuild:
            reusable_first_slide_id = prepare_existing_presentation_for_rebuild(slides, pres_id, run_id)
        pres = slides.presentations().get(presentationId=pres_id, fields="slides(objectId)").execute()
    else:
        pres = slides.presentations().create(body={"title": "Mini Media Deck (Styled)"}).execute()
        pres_id = pres["presentationId"]
        if run_state is not None:
            run_state["presentationId"] = pres_id
            run_state["presentationUrl"] = f"https://docs.google.com/presentation/d/{pres_id}/edit"
            save_run_state(run_state)
    page_w, page_h = fetch_presentation_page_size_pt(slides, pres_id)
    log_event(f"Foliensize erkannt: {page_w:.1f} x {page_h:.1f} pt.")
    if not existing_pres_id:
        first_slide_id = pres["slides"][0]["objectId"]
        initial_requests: List[dict] = [{"deleteObject": {"objectId": first_slide_id}}]
        retry_with_backoff(
            lambda: slides.presentations().batchUpdate(
                presentationId=pres_id,
                body={"requests": initial_requests},
            ).execute(),
            retries=MAX_RETRIES_PER_ITEM,
            run_id=run_id,
            phase="slides_batch_update_initial",
            item_index=-1,
        )

    slide_no = 0 if force_rebuild else int((run_state or {}).get("nextSlideNo", 0))
    completed_ids = set() if force_rebuild else set((run_state or {}).get("completedFileIds", []))
    for idx, item in enumerate(items):
        file_id = str(item.get("id", ""))
        mime = str(item.get("mimeType", ""))
        if not file_id:
            continue
        if file_id in completed_ids:
            if run_log is not None:
                run_log_step(run_log, "resume_skip", "ok", f"Datei übersprungen (bereits erledigt): {file_id}")
            continue
        # #region agent log
        debug_log(run_id, "H2", "webapp/app.py:build_styled_mini_presentation", "Slide item start", {"itemIndex": idx, "totalItems": len(items), "nextSlideNo": slide_no + 1, "mime": mime, "fileIdSuffix": file_id[-8:] if file_id else ""})
        # #endregion
        t_item_start = time.time()
        slide_no += 1
        if force_rebuild and idx == 0 and reusable_first_slide_id:
            slide_id = reusable_first_slide_id
            item_requests: List[dict] = []
        else:
            slide_id = f"slide_{slide_no:04d}"
            item_requests = [{"createSlide": {"objectId": slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}}]
        media_w, media_h, thumb_link = get_media_dimensions_and_thumb(drive, file_id)

        bg_bytes = b""
        if mime.startswith("image/"):
            bg_bytes = download_drive_file_bytes(access_token, file_id)
            # Reliable dimensions from original bytes to enforce exact centering.
            with Image.open(BytesIO(bg_bytes)) as src_img:
                src = ImageOps.exif_transpose(src_img)
                media_w, media_h = src.width, src.height
        elif mime.startswith("video/"):
            video_bytes = download_drive_file_bytes(access_token, file_id)
            bg_bytes = extract_first_video_frame(video_bytes)
            if not bg_bytes:
                bg_bytes = download_video_thumbnail(access_token, thumb_link)
        if not bg_bytes:
            neutral = Image.new("RGB", (1920, 1080), color=(20, 20, 20))
            bio = BytesIO()
            neutral.save(bio, format="JPEG", quality=90)
            bg_bytes = bio.getvalue()

        bg_name = f"bg_{file_id}.jpg"
        bg_id = find_background_file_id(drive, bg_folder_id, bg_name)
        if not bg_id:
            bg_id = retry_with_backoff(
                lambda: upload_generated_background(drive, bg_folder_id, bg_name, render_professional_background(bg_bytes)),
                retries=MAX_RETRIES_PER_ITEM,
                run_id=run_id,
                phase="upload_background",
                item_index=idx,
            )
        item_requests.append(
            {
                "createImage": {
                    "objectId": f"bg_{slide_no:04d}",
                    "url": f"https://drive.google.com/uc?id={bg_id}",
                    "elementProperties": {
                        "pageObjectId": slide_id,
                        "size": {
                            "width": {"magnitude": page_w, "unit": "PT"},
                            "height": {"magnitude": page_h, "unit": "PT"},
                        },
                        "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"},
                    },
                }
            }
        )

        left, top, draw_w, draw_h = compute_contain_box(media_w, media_h, page_w=page_w, page_h=page_h)
        if mime.startswith("image/"):
            item_requests.append(
                {
                    "createImage": {
                        "objectId": f"img_{slide_no:04d}",
                        "url": f"https://drive.google.com/uc?id={file_id}",
                        "elementProperties": {
                            "pageObjectId": slide_id,
                            "size": {
                                "width": {"magnitude": draw_w, "unit": "PT"},
                                "height": {"magnitude": draw_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": left, "translateY": top, "unit": "PT"},
                        },
                    }
                }
            )
        elif mime.startswith("video/"):
            # Erst Poster-Folie zeigen (Framebild), danach Video-Folie mit Autoplay.
            poster_name = f"poster_{file_id}.jpg"
            poster_id = find_background_file_id(drive, bg_folder_id, poster_name)
            if not poster_id:
                poster_bytes = bg_bytes
                if not poster_bytes:
                    poster_bytes = download_video_thumbnail(access_token, thumb_link)
                if not poster_bytes:
                    neutral = Image.new("RGB", (1920, 1080), color=(25, 25, 25))
                    out = BytesIO()
                    neutral.save(out, format="JPEG", quality=90)
                    poster_bytes = out.getvalue()
                poster_id = retry_with_backoff(
                    lambda: upload_generated_background(drive, bg_folder_id, poster_name, poster_bytes),
                    retries=MAX_RETRIES_PER_ITEM,
                    run_id=run_id,
                    phase="upload_video_poster",
                    item_index=idx,
                )
            item_requests.append(
                {
                    "createImage": {
                        "objectId": f"poster_{slide_no:04d}",
                        "url": f"https://drive.google.com/uc?id={poster_id}",
                        "elementProperties": {
                            "pageObjectId": slide_id,
                            "size": {
                                "width": {"magnitude": draw_w, "unit": "PT"},
                                "height": {"magnitude": draw_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": left, "translateY": top, "unit": "PT"},
                        },
                    }
                }
            )

            slide_no += 1
            video_slide_id = f"slide_{slide_no:04d}"
            item_requests.append(
                {"createSlide": {"objectId": video_slide_id, "slideLayoutReference": {"predefinedLayout": "BLANK"}}}
            )
            item_requests.append(
                {
                    "createImage": {
                        "objectId": f"bg_{slide_no:04d}",
                        "url": f"https://drive.google.com/uc?id={bg_id}",
                        "elementProperties": {
                            "pageObjectId": video_slide_id,
                            "size": {
                                "width": {"magnitude": page_w, "unit": "PT"},
                                "height": {"magnitude": page_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": 0, "translateY": 0, "unit": "PT"},
                        },
                    }
                }
            )
            item_requests.append(
                {
                    "createVideo": {
                        "objectId": f"vid_{slide_no:04d}",
                        "source": "DRIVE",
                        "id": file_id,
                        "elementProperties": {
                            "pageObjectId": video_slide_id,
                            "size": {
                                "width": {"magnitude": draw_w, "unit": "PT"},
                                "height": {"magnitude": draw_h, "unit": "PT"},
                            },
                            "transform": {"scaleX": 1, "scaleY": 1, "translateX": left, "translateY": top, "unit": "PT"},
                        },
                    }
                }
            )
            item_requests.append(
                {
                    "updateVideoProperties": {
                        "objectId": f"vid_{slide_no:04d}",
                        "videoProperties": {"autoPlay": True},
                        "fields": "autoPlay",
                    }
                }
            )

        # Split each item requests into smaller chunks for safer network calls.
        for start in range(0, len(item_requests), max(1, BATCH_REQUEST_SIZE)):
            chunk = item_requests[start : start + max(1, BATCH_REQUEST_SIZE)]
            retry_with_backoff(
                lambda chunk=chunk: slides.presentations().batchUpdate(
                    presentationId=pres_id,
                    body={"requests": chunk},
                ).execute(),
                retries=MAX_RETRIES_PER_ITEM,
                run_id=run_id,
                phase="slides_batch_update_item",
                item_index=idx,
            )

        if run_state is not None:
            run_state["lastCompletedItemIndex"] = idx
            run_state["nextSlideNo"] = slide_no
            run_state.setdefault("completedFileIds", []).append(file_id)
            run_state.setdefault("backgroundMap", {})[file_id] = bg_id
            run_state["forceRebuild"] = False
            save_run_state(run_state)

        # #region agent log
        debug_log(
            run_id,
            "H2",
            "webapp/app.py:build_styled_mini_presentation",
            "Slide item complete",
            {"itemIndex": idx, "durationMs": int((time.time() - t_item_start) * 1000), "slideNo": slide_no},
        )
        # #endregion
        if run_log is not None:
            run_log_step(
                run_log,
                "item_phase",
                "ok",
                f"itemIndex={idx} total={len(items)} phase=slide_update durationMs={int((time.time() - t_item_start) * 1000)}",
            )
        if (idx + 1) % max(1, CHECKPOINT_INTERVAL) == 0:
            log_event(f"Checkpoint: Item {idx + 1}/{len(items)} abgeschlossen.")

    return pres_id, slide_no


def _is_supported_media_mime(mime: str) -> bool:
    if mime.startswith("image/") and mime in SUPPORTED_IMAGE_MIME_TYPES:
        return True
    if mime.startswith("video/") and mime in SUPPORTED_VIDEO_MIME_TYPES:
        return True
    return False


def load_indexed_drive_media_items(index_file: Path, folder_names: Tuple[str, ...] = BASE_SOURCE_FOLDERS) -> List[dict]:
    if not index_file.exists():
        raise RuntimeError(f"Index-Datei nicht gefunden: {index_file}")
    payload = json.loads(index_file.read_text(encoding="utf-8"))
    folders = payload.get("folders", {})
    items: List[dict] = []
    for folder_name in folder_names:
        folder = folders.get(folder_name, {})
        folder_items = folder.get("items", [])
        for item in folder_items:
            mime = str(item.get("mimeType", ""))
            if not _is_supported_media_mime(mime):
                continue
            enriched = dict(item)
            enriched["folder"] = folder_name
            items.append(enriched)
    # Base order is deterministic; final capture-time order is resolved later.
    items.sort(key=lambda x: (x.get("name") or "", x.get("id") or ""))
    return items


def resolve_named_subfolder_id(drive, parent_id: str, folder_name: str) -> str:
    escaped = folder_name.replace("'", "\\'")
    query = (
        f"'{parent_id}' in parents and trashed=false and "
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{escaped}'"
    )
    files = (
        drive.files()
        .list(
            q=query,
            fields="files(id,name)",
            pageSize=5,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    if not files:
        raise RuntimeError(f"Ordner '{folder_name}' wurde unter dem Zielordner nicht gefunden.")
    if len(files) > 1:
        raise RuntimeError(f"Ordner '{folder_name}' ist mehrdeutig ({len(files)} Treffer). Bitte eindeutige Folder-ID angeben.")
    return str(files[0].get("id") or "")


def load_drive_media_items_from_folder(drive, folder_id: str, folder_name: str) -> List[dict]:
    query = f"'{folder_id}' in parents and trashed=false"
    items: List[dict] = []
    page_token = ""
    while True:
        res = (
            drive.files()
            .list(
                q=query,
                fields="nextPageToken,files(id,name,mimeType,createdTime,modifiedTime,size)",
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token or None,
            )
            .execute()
        )
        for item in res.get("files", []):
            mime = str(item.get("mimeType", ""))
            if not _is_supported_media_mime(mime):
                continue
            enriched = dict(item)
            enriched["folder"] = folder_name
            items.append(enriched)
        page_token = str(res.get("nextPageToken") or "")
        if not page_token:
            break
    items.sort(key=lambda x: (x.get("name") or "", x.get("id") or ""))
    return items


def select_mini_items(indexed_items: List[dict], max_items: int = 6) -> List[dict]:
    if not indexed_items:
        raise RuntimeError("Keine vorhandenen Drive-Medien für Mini-Test gefunden.")
    videos = [x for x in indexed_items if str(x.get("mimeType", "")).startswith("video/")]
    images = [x for x in indexed_items if str(x.get("mimeType", "")).startswith("image/")]
    selected: List[dict] = []
    has_video = bool(videos)
    # Wenn ein Video enthalten ist, planen wir 1 extra Slide (Poster + Video),
    # deshalb nur 5 Basis-Items für insgesamt max. 6 Slides.
    target_items = max_items - 1 if has_video else max_items

    if has_video:
        selected.append(videos[0])
    for item in images + videos[1:]:
        if len(selected) >= target_items:
            break
        selected.append(item)
    if len(selected) < target_items:
        for item in indexed_items:
            if len(selected) >= target_items:
                break
            if item in selected:
                continue
            selected.append(item)
    return selected[:target_items]


def ensure_image_files_public_readable(drive, indexed_items: List[dict]) -> int:
    changed = 0
    for item in indexed_items:
        mime_type = str(item.get("mimeType", ""))
        if not mime_type.startswith("image/"):
            continue
        file_id = str(item.get("id", ""))
        if not file_id:
            continue
        permissions = (
            drive.permissions()
            .list(fileId=file_id, fields="permissions(id,type,role)", supportsAllDrives=True)
            .execute()
            .get("permissions", [])
        )
        already_public = any(p.get("type") == "anyone" and p.get("role") in ("reader", "commenter", "writer") for p in permissions)
        if already_public:
            continue
        drive.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
            supportsAllDrives=True,
            sendNotificationEmail=False,
        ).execute()
        changed += 1
    return changed


def build_coverage_report(expected_items: List[dict], processed_ids: List[str]) -> dict:
    expected_ids = [str(x.get("id", "")) for x in expected_items if x.get("id")]
    expected_set = set(expected_ids)
    processed_set = set(processed_ids)
    processed_counter = Counter(processed_ids)
    missing = sorted(expected_set - processed_set)
    duplicates = sorted([k for k, v in processed_counter.items() if v > 1])
    unused = sorted(processed_set - expected_set)
    expected_images = [str(x.get("id", "")) for x in expected_items if str(x.get("mimeType", "")).startswith("image/")]
    expected_videos = [str(x.get("id", "")) for x in expected_items if str(x.get("mimeType", "")).startswith("video/")]
    missing_images = sorted(set(expected_images) - processed_set)
    missing_videos = sorted(set(expected_videos) - processed_set)
    return {
        "expectedCount": len(expected_ids),
        "processedCount": len(processed_ids),
        "missingMedia": missing,
        "missingImages": missing_images,
        "missingVideos": missing_videos,
        "duplicateMedia": duplicates,
        "unusedInDeck": unused,
    }


def build_mini_manifest(manifest: dict) -> dict:
    entries = manifest.get("entries", [])
    if not entries:
        raise RuntimeError("Manifest enthält keine Einträge.")

    selected: List[dict] = []
    video_entry = next((e for e in entries if e.get("type") == "video"), None)
    if video_entry:
        poster = next(
            (
                e
                for e in entries
                if e.get("type") == "video_poster"
                and e.get("sourcePath") == video_entry.get("sourcePath")
            ),
            None,
        )
        if poster:
            selected.append(dict(poster))
        selected.append(dict(video_entry))

    for entry in entries:
        if len(selected) >= 6:
            break
        if entry.get("type") not in ("image", "video_poster", "video"):
            continue
        if any(s.get("file") == entry.get("file") and s.get("type") == entry.get("type") for s in selected):
            continue
        selected.append(dict(entry))

    if len(selected) < 6:
        raise RuntimeError("Zu wenige geeignete Manifest-Einträge für den Mini-Test.")

    selected = selected[:6]
    for idx, entry in enumerate(selected):
        entry["slideIndex"] = idx
    return {"entries": selected}


def split_manifest_into_size_chunks(package_dir: Path, manifest: dict, max_chunk_mb: int) -> List[dict]:
    max_bytes = max(1, max_chunk_mb) * 1024 * 1024
    entries = manifest.get("entries", [])
    chunks: List[List[dict]] = []
    current_chunk: List[dict] = []
    current_bytes = 0
    current_files: set[str] = set()

    def entry_unique_bytes(entry: dict, known_files: set[str]) -> int:
        rel = entry.get("file")
        if not rel or rel in known_files:
            return 0
        path = package_dir / rel
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    for entry in entries:
        rel = entry.get("file")
        additional_bytes = entry_unique_bytes(entry, current_files)
        if current_chunk and current_bytes + additional_bytes > max_bytes:
            chunks.append(current_chunk)
            current_chunk = []
            current_bytes = 0
            current_files = set()
            additional_bytes = entry_unique_bytes(entry, current_files)

        current_chunk.append(dict(entry))
        if rel and rel not in current_files:
            current_files.add(rel)
            current_bytes += additional_bytes

    if current_chunk:
        chunks.append(current_chunk)

    normalized: List[dict] = []
    for chunk_entries in chunks:
        for idx, chunk_entry in enumerate(chunk_entries):
            chunk_entry["slideIndex"] = idx
        normalized.append({"entries": chunk_entries})
    return normalized


app = Flask(__name__)


@app.get("/")
def index():
    login_ok = request.args.get("login") == "ok"
    return render_template_string(
        HTML,
        folder_id=DEFAULT_FOLDER_ID,
        hub_base=DEFAULT_AUTH_HUB_BASE,
        target_app=DEFAULT_TARGET_APP,
        broker_base=DEFAULT_BROKER_BASE,
        chunk_size_mb=DEFAULT_CHUNK_SIZE_MB,
        login_ok=login_ok,
    )


@app.get("/auth/callback")
def auth_callback():
    target_app = request.args.get("targetApp", DEFAULT_TARGET_APP)
    oauth_session = request.args.get("oauthSession", "")
    broker_token = request.args.get("brokerToken", "")
    if oauth_session or broker_token:
        save_auth_state({"targetApp": target_app, "oauthSessionId": oauth_session, "brokerToken": broker_token})
        log_event("OAuth Callback erfolgreich: Session und Broker-Token gespeichert.")
    return redirect("/?login=ok")


@app.get("/auth/start")
def auth_start():
    hub_base = (request.args.get("brokerBase") or DEFAULT_BROKER_BASE).rstrip("/")
    target_app = (request.args.get("targetApp") or DEFAULT_TARGET_APP).strip()
    try:
        log_event(f"OAuth Start angefordert für App '{target_app}'.")
        url = auth_hub_oauth_url(
            hub_base=hub_base, target_app=target_app, return_to=f"http://127.0.0.1:{WEBAPP_PORT}/auth/callback"
        )
        return jsonify({"ok": True, "url": url})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/auth/apps")
def auth_apps():
    hub_base = (request.args.get("brokerBase") or DEFAULT_BROKER_BASE).rstrip("/")
    try:
        return jsonify(auth_hub_apps(hub_base))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/auth/status")
def auth_status():
    hub_base = (request.args.get("brokerBase") or DEFAULT_BROKER_BASE).rstrip("/")
    target_app = (request.args.get("targetApp") or DEFAULT_TARGET_APP).strip()
    reachable = True
    error = None
    try:
        requests.get(f"{hub_base}/health", timeout=10).raise_for_status()
    except Exception as exc:
        reachable = False
        error = str(exc)

    token_ok = False
    token_error = None
    if reachable:
        try:
            token = auth_hub_access_token(hub_base, target_app)
            token_ok = bool(token)
            if token_ok:
                log_event("Auth-Status: Token ist bereit.")
        except Exception as exc:
            token_error = str(exc)
            log_event(f"Auth-Status: Tokenfehler ({token_error}).")

    return jsonify(
        {
            "ok": True,
            "hubBase": hub_base,
            "targetApp": target_app,
            "hubReachable": reachable,
            "hubError": error,
            "tokenReady": token_ok,
            "tokenError": token_error,
            "oauthSessionIdPresent": bool(load_auth_state().get("oauthSessionId")),
            "brokerTokenPresent": bool(load_auth_state().get("brokerToken")),
        }
    )


@app.post("/run")
def run_pipeline():
    body = request.get_json(force=True) or {}
    folder_id = body.get("folderId") or DEFAULT_FOLDER_ID
    hub_base = (body.get("brokerBase") or DEFAULT_BROKER_BASE).rstrip("/")
    target_app = (body.get("targetApp") or DEFAULT_TARGET_APP).strip()
    target_presentation_id = (body.get("presentationId") or DEFAULT_EXISTING_PRESENTATION_ID).strip()
    run_log = new_run_log(mode="run", target_app=target_app, target_folder_id=folder_id)
    access_token = ""
    run_state = {}
    try:
        log_event("Voller Publish-Lauf gestartet.")
        run_log_step(run_log, "start", "ok", "Voller Publish-Lauf gestartet.")
        # #region agent log
        debug_log(run_log["runId"], "H3", "webapp/app.py:run_pipeline", "Run start", {"mode": "run", "targetFolderId": folder_id})
        # #endregion
        access_token = auth_hub_access_token(hub_base, target_app)
        run_log_step(run_log, "auth", "ok", "Access Token erfolgreich geladen.")
        resume_state = load_resume_run_state(folder_id)
        if resume_state:
            run_log_step(
                run_log,
                "resume",
                "ok",
                f"Resume aktiv ab Item {int(resume_state.get('lastCompletedItemIndex', -1)) + 1}",
            )
            log_event(f"Resume aktiv ab Position {int(resume_state.get('lastCompletedItemIndex', -1)) + 1}.")
            run_state = resume_state
            run_state["status"] = "running"
            run_state["resumedAt"] = utc_iso_now()
        else:
            run_state = {
                "runId": run_log["runId"],
                "targetFolderId": folder_id,
                "mode": "run",
                "status": "running",
                "orderedSequence": [],
                "lastCompletedItemIndex": -1,
                "nextSlideNo": 0,
                "completedFileIds": [],
                "backgroundMap": {},
                "presentationId": target_presentation_id,
                "forceRebuild": True,
            }
            save_run_state(run_state)
        creds = Credentials(token=access_token, scopes=SCOPES)
        drive = build("drive", "v3", credentials=creds)
        run_log_step(run_log, "resolve_primary_folder", "ok", f"Suche Ordner '{PRIMARY_FOLDER_NAME}' unter Zielordner.")
        primary_folder_id = resolve_named_subfolder_id(drive, folder_id, PRIMARY_FOLDER_NAME)
        run_log_step(run_log, "resolve_primary_folder", "ok", f"Ordner '{PRIMARY_FOLDER_NAME}' gefunden: {primary_folder_id}")

        indexed_items = load_indexed_drive_media_items(DRIVE_INDEX_FILE, folder_names=BASE_SOURCE_FOLDERS)
        indexed_items = enrich_and_sort_items_by_capture_time(indexed_items, access_token, run_log["runId"], drive)

        primary_items = load_drive_media_items_from_folder(drive, primary_folder_id, PRIMARY_FOLDER_NAME)
        primary_items = enrich_and_sort_items_by_capture_time(primary_items, access_token, run_log["runId"], drive)

        # Lena-Block immer vorne, danach bestehender Block unverändert.
        combined_items: List[dict] = []
        seen_ids: set[str] = set()
        for bucket in (primary_items, indexed_items):
            for item in bucket:
                fid = str(item.get("id", ""))
                if not fid or fid in seen_ids:
                    continue
                seen_ids.add(fid)
                combined_items.append(item)

        run_state["orderedSequence"] = [str(x.get("id", "")) for x in combined_items if x.get("id")]
        save_run_state(run_state)
        if combined_items:
            oldest = combined_items[0]
            newest = combined_items[-1]
            log_event(
                f"Sortierung Capture-Zeit: ältestes={oldest.get('name')} ({oldest.get('captureTime')}), "
                f"neuestes={newest.get('name')} ({newest.get('captureTime')})."
            )
        if primary_items:
            log_event(
                f"Prepend aktiv: {len(primary_items)} Medien aus {PRIMARY_FOLDER_NAME} am Anfang "
                f"({primary_items[0].get('captureTime')} -> {primary_items[-1].get('captureTime')})."
            )
        # #region agent log
        debug_log(run_log["runId"], "H4", "webapp/app.py:run_pipeline", "Indexed items loaded", {"count": len(combined_items), "primaryCount": len(primary_items)})
        # #endregion
        run_log_step(
            run_log,
            "index",
            "ok",
            f"{len(combined_items)} Medien geladen (Highlights={len(primary_items)}, Bestand={len(indexed_items)}).",
        )
        changed_permissions = ensure_image_files_public_readable(drive, combined_items)
        if changed_permissions:
            log_event(f"{changed_permissions} Bilddateien wurden für Slides auf 'anyone reader' gesetzt.")
        # #region agent log
        debug_log(run_log["runId"], "H3", "webapp/app.py:run_pipeline", "Permissions update complete", {"changedPermissions": changed_permissions})
        # #endregion
        run_log_step(run_log, "permissions", "ok", f"Bildfreigaben aktualisiert: {changed_permissions}")
        slides = build("slides", "v1", credentials=creds)
        pres_id, slide_count = build_styled_mini_presentation(
            drive,
            slides,
            access_token,
            folder_id,
            combined_items,
            run_id=run_log["runId"],
            run_state=run_state,
            run_log=run_log,
        )
        presentation_url = f"https://docs.google.com/presentation/d/{pres_id}/edit"
        log_event(f"Publiziert: {presentation_url}")
        run_log["publishTargets"] = [{"presentationId": pres_id, "presentationUrl": presentation_url, "slidesCreated": slide_count}]
        run_log["completedItems"] = [
            "Auth erfolgreich",
            "Index geladen",
            "Slides erstellt",
        ]
        run_log["nextActions"] = ["Präsentation visuell prüfen", "Reihenfolge prüfen"]
        run_log["rejectedHypothesisCandidates"] = ["H4"]
        run_log["inconclusiveHypothesisCandidates"] = ["H1", "H3", "H5"]
        run_log["nextDiagnosticActions"] = ["Bei erneutem EOF Resume fortsetzen und Retry-Events im Debug-Log prüfen"]
        coverage = build_coverage_report(combined_items, run_state.get("completedFileIds", []))
        run_log["coverageAudit"] = coverage
        run_log["prependMode"] = "primary_block_first"
        run_log["primaryFolderStats"] = {
            "count": len(primary_items),
            "firstCaptureTime": primary_items[0].get("captureTime") if primary_items else None,
            "lastCaptureTime": primary_items[-1].get("captureTime") if primary_items else None,
        }
        if coverage["missingMedia"]:
            log_event(f"Coverage: {len(coverage['missingMedia'])} Medien fehlen noch.")
        else:
            log_event("Coverage: Keine fehlenden Medien aus Highlights/Sample/Timo/AlbumC.")
        run_log["orderedMergeInstructions"] = [
            {
                "partIndex": 1,
                "presentationId": pres_id,
                "presentationUrl": presentation_url,
                "fromOrder": 1,
                "toOrder": slide_count,
            }
        ]
        run_log_step(run_log, "publish", "ok", f"Präsentation erstellt: {presentation_url}")
        run_log_complete(run_log, ok=True)
        run_state["status"] = "completed"
        run_state["presentationId"] = pres_id
        run_state["presentationUrl"] = presentation_url
        save_run_state(run_state)
        persist_run_log_to_drive(access_token, folder_id, run_log)
        return jsonify(
            {
                "ok": True,
                "mode": "existing_drive_media_styled",
                "slidesCreated": slide_count,
                "presentationUrl": presentation_url,
                "presentationId": pres_id,
                "sourceFolders": [PRIMARY_FOLDER_NAME, *BASE_SOURCE_FOLDERS],
                "coverageAudit": coverage,
                "runLog": {"runId": run_log["runId"], "status": run_log["status"]},
            }
        )
    except Exception as exc:
        log_event(f"Voller Publish-Lauf fehlgeschlagen: {exc}")
        # #region agent log
        debug_log(
            run_log["runId"],
            "H1",
            "webapp/app.py:run_pipeline",
            "Run failed",
            {"errorType": type(exc).__name__, "error": str(exc)[:350], "traceTail": traceback.format_exc()[-1200:]},
        )
        # #endregion
        run_log_step(run_log, "error", "error", str(exc))
        run_log["nextActions"] = ["Fehler beheben", "Lauf erneut starten"]
        run_log["confirmedHypothesisCandidates"] = ["H1"] if is_transient_error(exc) else []
        run_log["inconclusiveHypothesisCandidates"] = ["H2", "H3", "H5"]
        run_log["nextDiagnosticActions"] = ["Resume erneut starten", "letzte Retry-/Phase-Logs analysieren"]
        run_log_complete(run_log, ok=False, error=str(exc))
        if run_state:
            run_state["status"] = "interrupted"
            run_state["lastError"] = str(exc)
            run_state["interruptedAt"] = utc_iso_now()
            try:
                save_run_state(run_state)
            except Exception:
                pass
        if access_token:
            try:
                persist_run_log_to_drive(access_token, folder_id, run_log)
            except Exception as log_exc:
                log_event(f"Drive-Log konnte nicht geschrieben werden: {log_exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/run-mini")
def run_pipeline_mini():
    body = request.get_json(force=True) or {}
    folder_id = body.get("folderId") or DEFAULT_FOLDER_ID
    target_app = (body.get("targetApp") or DEFAULT_TARGET_APP).strip()
    broker_base = (body.get("brokerBase") or DEFAULT_BROKER_BASE).rstrip("/")
    run_log = new_run_log(mode="run-mini", target_app=target_app, target_folder_id=folder_id)
    access_token = ""
    try:
        log_event("Mini-Presentation-Test gestartet (6 Slides mit mindestens einem Video).")
        run_log_step(run_log, "start", "ok", "Mini-Test gestartet.")
        access_token = auth_hub_access_token(broker_base, target_app)
        run_log_step(run_log, "auth", "ok", "Access Token erfolgreich geladen.")
        creds = Credentials(token=access_token, scopes=SCOPES)
        drive = build("drive", "v3", credentials=creds)
        indexed_items = load_indexed_drive_media_items(DRIVE_INDEX_FILE)
        indexed_items = enrich_and_sort_items_by_capture_time(indexed_items, access_token, run_log["runId"], drive)
        mini_items = select_mini_items(indexed_items, max_items=6)
        run_log_step(run_log, "selection", "ok", f"{len(mini_items)} Medien für Mini-Test ausgewählt.")
        changed_permissions = ensure_image_files_public_readable(drive, mini_items)
        if changed_permissions:
            log_event(f"Mini-Test: {changed_permissions} Bilddateien wurden auf 'anyone reader' gesetzt.")
        run_log_step(run_log, "permissions", "ok", f"Bildfreigaben aktualisiert: {changed_permissions}")
        slides = build("slides", "v1", credentials=creds)
        pres_id, slide_count = build_styled_mini_presentation(
            drive,
            slides,
            access_token,
            folder_id,
            mini_items,
            run_id=run_log["runId"],
            run_log=run_log,
        )
        log_event("Mini-Presentation-Test erfolgreich erstellt.")
        presentation_url = f"https://docs.google.com/presentation/d/{pres_id}/edit"
        log_event(f"Publiziert: {presentation_url}")
        run_log["publishTargets"] = [{"presentationId": pres_id, "presentationUrl": presentation_url, "slidesCreated": slide_count}]
        run_log["completedItems"] = ["Auth erfolgreich", "Mini-Selektion erstellt", "Mini-Publish erfolgreich"]
        run_log["nextActions"] = ["Ergebnis prüfen", "Bei OK auf Voll-Lauf anwenden"]
        run_log_step(run_log, "publish", "ok", f"Mini-Präsentation erstellt: {presentation_url}")
        run_log_complete(run_log, ok=True)
        persist_run_log_to_drive(access_token, folder_id, run_log)
        return jsonify(
            {
                "ok": True,
                "mode": "mini_test_styled_media",
                "slidesCreated": slide_count,
                "presentationUrl": presentation_url,
                "presentationId": pres_id,
                "runLog": {"runId": run_log["runId"], "status": run_log["status"]},
            }
        )
    except Exception as exc:
        log_event(f"Mini-Presentation-Test fehlgeschlagen: {exc}")
        run_log_step(run_log, "error", "error", str(exc))
        run_log["nextActions"] = ["Fehler beheben", "Mini-Test erneut ausführen"]
        run_log_complete(run_log, ok=False, error=str(exc))
        if access_token:
            try:
                persist_run_log_to_drive(access_token, folder_id, run_log)
            except Exception as log_exc:
                log_event(f"Drive-Log konnte nicht geschrieben werden: {log_exc}")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/logs")
def get_logs():
    return jsonify({"ok": True, "logs": RUNTIME_LOGS[-200:]})


if __name__ == "__main__":
    app.run(port=WEBAPP_PORT, debug=True)
