import json
import os
import time
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote_plus

from flask import Flask, jsonify, redirect, request
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
APPS_FILE = BASE_DIR / "apps.json"
CLIENT_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = STATE_DIR / "tokens.json"

AUTH_HUB_BASE = os.environ.get("AUTH_HUB_BASE", "http://127.0.0.1:8790").rstrip("/")
AUTH_HUB_PORT = int(os.environ.get("AUTH_HUB_PORT", "8790"))

app = Flask(__name__)


def ensure_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text("{}", encoding="utf-8")


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def apps_registry() -> Dict[str, dict]:
    raw = load_json(APPS_FILE, {"apps": []})
    result: Dict[str, dict] = {}
    for app_item in raw.get("apps", []):
        app_id = app_item.get("appId")
        if app_id:
            result[app_id] = app_item
    return result


def token_store() -> Dict[str, dict]:
    ensure_state()
    return load_json(TOKEN_FILE, {})


def set_token_for_app(app_id: str, token_data: dict) -> None:
    store = token_store()
    store[app_id] = token_data
    save_json(TOKEN_FILE, store)


def get_token_for_app(app_id: str) -> dict:
    return token_store().get(app_id, {})


def token_expired(token_data: dict) -> bool:
    expiry = token_data.get("expiry_date")
    if not expiry:
        return False
    try:
        return int(expiry) <= int(time.time() * 1000) + 60_000
    except Exception:
        return False


def build_flow(scopes: list[str], state: str) -> Flow:
    if not CLIENT_FILE.exists():
        raise RuntimeError(
            f"Fehlende OAuth-Client-Datei: {CLIENT_FILE}. "
            "Bitte einmalig als Google OAuth Desktop-Client hinterlegen."
        )
    flow = Flow.from_client_secrets_file(str(CLIENT_FILE), scopes=scopes, state=state)
    flow.redirect_uri = f"{AUTH_HUB_BASE}/oauth/callback"
    return flow


def has_local_oauth_client() -> bool:
    return CLIENT_FILE.exists()


def parse_state(state: str) -> dict:
    try:
        return json.loads(state)
    except Exception:
        return {}


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "authHubBase": AUTH_HUB_BASE,
            "localClientConfigured": has_local_oauth_client(),
        }
    )


@app.get("/apps")
def list_apps():
    reg = apps_registry()
    payload = []
    for app_id, app_item in reg.items():
        payload.append(
            {
                "appId": app_id,
                "appName": app_item.get("appName", app_id),
                "scopes": app_item.get("scopes", []),
                "defaultReturnTo": app_item.get("defaultReturnTo", ""),
            }
        )
    return jsonify({"ok": True, "apps": payload})


@app.get("/oauth/start")
def oauth_start():
    try:
        target_app = (request.args.get("targetApp") or "").strip()
        return_to = (request.args.get("returnTo") or "").strip()
        reg = apps_registry()
        app_cfg = reg.get(target_app)
        if not app_cfg:
            return jsonify({"ok": False, "error": f"Unbekannte targetApp: {target_app}"}), 400

        if not return_to:
            return_to = app_cfg.get("defaultReturnTo", "")

        if not has_local_oauth_client():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": (
                            f"Fehlende OAuth-Client-Datei: {CLIENT_FILE}. "
                            "Bitte auth-hub/credentials.json hinterlegen und "
                            "Redirect URI http://127.0.0.1:8790/oauth/callback in Google Cloud eintragen."
                        ),
                    }
                ),
                500,
            )

        state_payload = {"targetApp": target_app, "returnTo": return_to}
        flow = build_flow(app_cfg.get("scopes", []), state=json.dumps(state_payload))
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        return jsonify({"ok": True, "mode": "local", "url": auth_url, "targetApp": target_app, "returnTo": return_to})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/oauth/callback")
def oauth_callback():
    try:
        code = request.args.get("code")
        raw_state = request.args.get("state", "")
        state_data = parse_state(raw_state)
        target_app = state_data.get("targetApp", "")
        return_to = state_data.get("returnTo", "")
        reg = apps_registry()
        app_cfg = reg.get(target_app)
        if not code or not app_cfg:
            return jsonify({"ok": False, "error": "Ungültiger Callback (code/targetApp fehlt)."}), 400

        flow = build_flow(app_cfg.get("scopes", []), state=raw_state)
        flow.fetch_token(code=code)
        creds = flow.credentials
        token_data = {
            "access_token": creds.token,
            "refresh_token": creds.refresh_token,
            "expiry_date": int(creds.expiry.timestamp() * 1000) if creds.expiry else None,
            "scopes": creds.scopes or app_cfg.get("scopes", []),
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }
        old = get_token_for_app(target_app)
        if old.get("refresh_token") and not token_data.get("refresh_token"):
            token_data["refresh_token"] = old["refresh_token"]
        set_token_for_app(target_app, token_data)

        if return_to:
            sep = "&" if "?" in return_to else "?"
            return redirect(f"{return_to}{sep}oauth=ok&targetApp={quote_plus(target_app)}")

        return (
            "<h3>OAuth erfolgreich.</h3>"
            f"<p>targetApp: <b>{target_app}</b></p>"
            "<p>Du kannst das Fenster schließen und in die Ziel-App zurückkehren.</p>"
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/token")
def get_token():
    target_app = (request.args.get("targetApp") or "").strip()
    reg = apps_registry()
    if target_app not in reg:
        return jsonify({"ok": False, "error": f"Unbekannte targetApp: {target_app}"}), 400

    token_data = get_token_for_app(target_app)
    if not token_data:
        return jsonify({"ok": False, "error": "Kein Token vorhanden. Bitte /oauth/start ausführen."}), 404

    if token_data.get("access_token") and not token_expired(token_data):
        return jsonify(
            {
                "ok": True,
                "access_token": token_data.get("access_token"),
                "expiry_date": token_data.get("expiry_date"),
            }
        )

    if token_data.get("refresh_token"):
        creds = Credentials(
            token=token_data.get("access_token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes"),
        )
        creds.refresh(GoogleRequest())
        token_data["access_token"] = creds.token
        token_data["expiry_date"] = int(creds.expiry.timestamp() * 1000) if creds.expiry else None
        if creds.refresh_token:
            token_data["refresh_token"] = creds.refresh_token
        set_token_for_app(target_app, token_data)
        return jsonify({"ok": True, "access_token": creds.token, "expiry_date": token_data["expiry_date"]})

    return jsonify({"ok": False, "error": "Token abgelaufen und kein refresh_token vorhanden."}), 401


@app.post("/token/revoke")
def revoke_token():
    body = request.get_json(force=True) or {}
    target_app = (body.get("targetApp") or "").strip()
    store = token_store()
    if target_app in store:
        del store[target_app]
        save_json(TOKEN_FILE, store)
    return jsonify({"ok": True, "revoked": target_app})


if __name__ == "__main__":
    ensure_state()
    app.run(host="127.0.0.1", port=AUTH_HUB_PORT, debug=True)
