"""
SR Controller Server — Advanced
"""

from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
from datetime import datetime
from typing import Optional
import secrets
import uuid
import os

app = FastAPI(title="SR Controller Server", version="2.0")

devices = {}
pair_codes = {}
command_queue = {}
received_files = {}

UPLOAD_DIR = "/tmp/srcontroller_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


class RegisterPayload(BaseModel):
    role: str
    device_name: Optional[str] = "unknown"
    pair_code: Optional[str] = None


class RegisterResponse(BaseModel):
    device_id: str
    token: str
    pair_code: Optional[str] = None
    role: str


class LocationPayload(BaseModel):
    lat: float
    lon: float
    timestamp: Optional[int] = None


class LocationResponse(BaseModel):
    lat: float
    lon: float
    timestamp: str


class CommandResponse(BaseModel):
    command: Optional[str] = None


class FileInfo(BaseModel):
    filename: str
    url: str
    size: int
    type: str
    timestamp: str


def generate_pair_code():
    for _ in range(100):
        code = secrets.token_hex(3).upper()
        if code not in pair_codes:
            return code
    raise HTTPException(500, "Pair code generate nahi hua")


def get_device_by_token(token: str):
    for d in devices.values():
        if d["token"] == token:
            return d
    return None


def require_auth(authorization: Optional[str], role: Optional[str] = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization")
    token = authorization.replace("Bearer ", "")
    device = get_device_by_token(token)
    if not device:
        raise HTTPException(401, "Invalid token")
    if role and device["role"] != role:
        raise HTTPException(403, f"Sirf {role} kar sakta hai")
    return device


@app.get("/")
def root():
    return {"status": "SR Controller Server is running", "version": "2.0", "devices": len(devices)}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/register", response_model=RegisterResponse)
def register(payload: RegisterPayload):
    role = payload.role.lower()
    if role not in ("child", "parent"):
        raise HTTPException(400, "role child ya parent hona chahiye")

    device_id = str(uuid.uuid4())[:8]
    token = secrets.token_urlsafe(32)

    if role == "child":
        pair_code = generate_pair_code()
        devices[device_id] = {
            "device_id": device_id, "device_name": payload.device_name,
            "role": "child", "token": token, "pair_code": pair_code,
            "last_location": None,
            "registered_at": datetime.now().isoformat(),
        }
        pair_codes[pair_code] = device_id
        command_queue[device_id] = []
        received_files[device_id] = []
        return RegisterResponse(device_id=device_id, token=token, pair_code=pair_code, role="child")

    if not payload.pair_code:
        raise HTTPException(400, "parent ke liye pair_code chahiye")
    child_id = pair_codes.get(payload.pair_code.upper())
    if not child_id:
        raise HTTPException(404, "pair_code galat")

    devices[device_id] = {
        "device_id": device_id, "device_name": payload.device_name,
        "role": "parent", "token": token, "paired_child": child_id,
        "registered_at": datetime.now().isoformat(),
    }
    return RegisterResponse(device_id=device_id, token=token, role="parent")


@app.post("/location")
def receive_location(payload: LocationPayload, authorization: str = Header(None)):
    device = require_auth(authorization, role="child")
    device["last_location"] = {
        "lat": payload.lat, "lon": payload.lon,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(f"[LOCATION] {device['device_name']} -> {device['last_location']}")
    return {"ok": True}


@app.get("/location/latest", response_model=LocationResponse)
def get_latest_location(authorization: str = Header(None)):
    parent = require_auth(authorization, role="parent")
    child = devices.get(parent["paired_child"])
    if not child or not child["last_location"]:
        return LocationResponse(lat=0.0, lon=0.0, timestamp="never")
    loc = child["last_location"]
    return LocationResponse(lat=loc["lat"], lon=loc["lon"], timestamp=loc["timestamp"])


@app.post("/command/{cmd}")
def push_command(cmd: str, authorization: str = Header(None)):
    parent = require_auth(authorization, role="parent")
    child_id = parent["paired_child"]
    command_queue.setdefault(child_id, []).append(cmd)
    print(f"[CMD] {parent['device_name']} -> {cmd}")
    return {"ok": True, "queued": cmd}


@app.get("/command", response_model=CommandResponse)
def get_pending_command(authorization: str = Header(None)):
    child = require_auth(authorization, role="child")
    q = command_queue.get(child["device_id"], [])
    if q:
        return CommandResponse(command=q.pop(0))
    return CommandResponse(command=None)


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    file_type: str = Form("unknown"),
    authorization: str = Header(None),
):
    child = require_auth(authorization, role="child")
    child_id = child["device_id"]

    safe_name = f"{child_id}_{int(datetime.now().timestamp())}_{file.filename}"
    path = os.path.join(UPLOAD_DIR, safe_name)
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)

    info = {
        "filename": safe_name,
        "url": f"/files/{safe_name}",
        "size": len(content),
        "type": file_type,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    received_files.setdefault(child_id, []).append(info)
    print(f"[FILE] {child['device_name']} -> {safe_name} ({len(content)} bytes)")
    return {"ok": True, "file": info}


@app.get("/files/{filename}")
def serve_file(filename: str, authorization: str = Header(None)):
    require_auth(authorization)
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "File not found")
    return FileResponse(path)


@app.get("/files/latest")
def latest_files(authorization: str = Header(None)):
    parent = require_auth(authorization, role="parent")
    child_id = parent["paired_child"]
    files = received_files.get(child_id, [])
    print(f"[LIST] {len(files)} files for {child_id}")
    return {"count": len(files), "files": files[-20:]}


@app.get("/admin/devices")
def list_devices():
    return {
        "count": len(devices),
        "devices": [
            {
                "device_id": d["device_id"], "name": d.get("device_name"),
                "role": d["role"], "pair_code": d.get("pair_code"),
                "last_location": d.get("last_location"),
                "paired_child": d.get("paired_child"),
            }
            for d in devices.values()
        ],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)