import base64
import binascii
import json
import os
import ipaddress
import queue
import secrets
import socket
import sqlite3
import threading
import time
import uuid
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATABASE_FILE = Path(
    os.environ.get("RUANG_LAYANAN_DATABASE", ROOT / "notifications.db")
).resolve()
LEGACY_DATA_FILE = Path(
    os.environ.get("RUANG_LAYANAN_DATA", ROOT / "notifications.json")
).resolve()
RECEIVER_CODES_FILE = Path(
    os.environ.get("RUANG_LAYANAN_KODE_PENERIMA_FILE", ROOT / "kode-penerima.json")
).resolve()
HOST = os.environ.get("RUANG_LAYANAN_HOST", "0.0.0.0")
PORT = int(os.environ.get("RUANG_LAYANAN_PORT", os.environ.get("PORT", "8000")))
DEPARTMENTS = {"KCP", "ROP/BM", "CS", "TELLER", "AOP 1", "AOP 2", "PCO", "COP"}
DEPARTMENT_NAMES = ("KCP", "ROP/BM", "CS", "TELLER", "AOP 1", "AOP 2", "PCO", "COP")
MAX_NOTIFICATION_PHOTO_BYTES = 5 * 1024 * 1024
MAX_NOTIFICATION_REQUEST_BYTES = 7 * 1024 * 1024
MAX_BULK_DELETE_REQUEST_BYTES = 256 * 1024
data_lock = threading.Lock()
subscribers = {}
sessions = {}
public_deployment = os.environ.get("RUANG_LAYANAN_PUBLIC_DEPLOYMENT") == "1"
secure_cookies = os.environ.get("RUANG_LAYANAN_COOKIE_SECURE", "").lower() in {
    "1", "true", "yes"
}
guard_access_code = os.environ.get(
    "RUANG_LAYANAN_KODE_PENGIRIM",
    "" if public_deployment else "SATPAM2026",
).strip()
if public_deployment and not secure_cookies:
    raise RuntimeError(
        "Hosting publik wajib menggunakan HTTPS dan "
        "RUANG_LAYANAN_COOKIE_SECURE=1."
    )
if not guard_access_code:
    raise RuntimeError(
        "Atur RUANG_LAYANAN_KODE_PENGIRIM sebelum menjalankan hosting publik."
        if public_deployment
        else "Kode akses pengirim tidak boleh kosong."
    )
if public_deployment and (
    len(guard_access_code) < 16
    or guard_access_code.upper().startswith(("GANTI_", "REPLACE_", "CHANGE_"))
):
    raise RuntimeError(
        "Kode akses pengirim hosting publik harus kode acak minimal 16 karakter."
    )


def load_receiver_access_codes():
    configured_codes = os.environ.get("RUANG_LAYANAN_KODE_PENERIMA_JSON")
    try:
        if configured_codes is not None:
            codes = json.loads(configured_codes)
        else:
            with RECEIVER_CODES_FILE.open("r", encoding="utf-8") as codes_file:
                codes = json.load(codes_file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Gagal membaca RUANG_LAYANAN_KODE_PENERIMA_JSON."
            if configured_codes is not None
            else f"Gagal membaca konfigurasi kode penerima {RECEIVER_CODES_FILE}: {error}"
        ) from error
    if not isinstance(codes, dict) or set(codes) != DEPARTMENTS:
        raise RuntimeError(
            f"Konfigurasi {RECEIVER_CODES_FILE} harus berisi satu kode untuk setiap unit: "
            f"{', '.join(DEPARTMENT_NAMES)}."
        )
    values = tuple(codes.values())
    if any(
        not isinstance(code, str) or len(code) < 6 or code != code.strip()
        for code in values
    ):
        raise RuntimeError("Setiap kode penerima harus berupa teks minimal 6 karakter.")
    if len({code.upper() for code in values}) != len(values):
        raise RuntimeError("Setiap unit kerja harus memiliki kode penerima yang berbeda.")
    if public_deployment and (
        configured_codes is None
        or any(
            len(code) < 16
            or code.upper().startswith(("GANTI_", "REPLACE_", "CHANGE_"))
            for code in values
        )
    ):
        raise RuntimeError(
            "Hosting publik wajib memakai RUANG_LAYANAN_KODE_PENERIMA_JSON "
            "dengan kode acak minimal 16 karakter untuk setiap unit."
        )
    return codes


receiver_access_codes = load_receiver_access_codes()


def validate_notification_photo(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("Format foto tidak valid.")
    header, separator, encoded_data = value.partition(",")
    content_type = {
        "data:image/jpeg;base64": "image/jpeg",
        "data:image/png;base64": "image/png",
        "data:image/webp;base64": "image/webp",
    }.get(header)
    if not separator or content_type is None:
        raise ValueError("Foto harus berformat JPG, PNG, atau WebP.")
    try:
        photo_data = base64.b64decode(encoded_data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Data foto tidak valid.") from error
    if not photo_data or len(photo_data) > MAX_NOTIFICATION_PHOTO_BYTES:
        raise ValueError("Ukuran foto maksimal 5 MB.")
    valid_signatures = {
        "image/jpeg": photo_data.startswith(b"\xff\xd8\xff"),
        "image/png": photo_data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": (
            len(photo_data) >= 12
            and photo_data.startswith(b"RIFF")
            and photo_data[8:12] == b"WEBP"
        ),
    }
    if not valid_signatures[content_type]:
        raise ValueError("Isi foto tidak sesuai dengan format berkasnya.")
    return content_type, photo_data


def validate_customer_name(value):
    if not isinstance(value, str):
        raise ValueError("Nama nasabah hanya boleh berisi huruf dan spasi.")
    name = value.strip()
    if len(name) > 200:
        raise ValueError("Nama nasabah melebihi batas 200 karakter.")
    previous_character_is_letter = False
    for character in name:
        if character == " ":
            previous_character_is_letter = False
        elif character.isalpha():
            previous_character_is_letter = True
        elif (
            unicodedata.category(character).startswith("M")
            and previous_character_is_letter
        ):
            continue
        else:
            raise ValueError("Nama nasabah hanya boleh berisi huruf dan spasi.")
    return name


def connect_database():
    try:
        DATABASE_FILE.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(DATABASE_FILE, timeout=10)
    except (OSError, sqlite3.Error) as error:
        raise RuntimeError(f"Gagal membuka database {DATABASE_FILE}: {error}") from error


@contextmanager
def database_connection():
    connection = connect_database()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database():
    try:
        with database_connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    customer_name TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    purpose TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_photos (
                    notification_id TEXT PRIMARY KEY,
                    content_type TEXT NOT NULL,
                    image_data BLOB NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at)"
            )
            imported = connection.execute(
                "SELECT value FROM app_metadata WHERE key = 'legacy_json_imported'"
            ).fetchone()
            if imported is None:
                if LEGACY_DATA_FILE.exists():
                    with LEGACY_DATA_FILE.open("r", encoding="utf-8") as data_file:
                        legacy_notifications = json.load(data_file)
                    if not isinstance(legacy_notifications, list):
                        raise ValueError("Data notifikasi lama harus berupa daftar.")
                    for item in legacy_notifications:
                        if not isinstance(item, dict):
                            raise ValueError("Data notifikasi lama memiliki format tidak valid.")
                    connection.executemany(
                        """
                        INSERT OR IGNORE INTO notifications
                            (id, customer_name, department, description, status, created_at, purpose)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                str(item["id"]),
                                str(item.get("customerName", "")),
                                str(item["department"]),
                                str(item["description"]),
                                str(item["status"]),
                                str(item["createdAt"]),
                                item.get("purpose"),
                            )
                            for item in legacy_notifications
                        ],
                    )
                connection.execute(
                    "INSERT INTO app_metadata (key, value) VALUES ('legacy_json_imported', 'true')"
                )
    except (OSError, json.JSONDecodeError, KeyError, ValueError, sqlite3.Error, RuntimeError) as error:
        raise RuntimeError(f"Gagal menyiapkan database {DATABASE_FILE}: {error}") from error


def load_notifications():
    initialize_database()
    try:
        with database_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    notifications.id,
                    customer_name,
                    department,
                    description,
                    status,
                    created_at,
                    purpose,
                    EXISTS(
                        SELECT 1
                        FROM notification_photos
                        WHERE notification_id = notifications.id
                    )
                FROM notifications
                ORDER BY created_at DESC, rowid DESC
                """
            ).fetchall()
        return [
            {
                "id": row[0],
                "customerName": row[1],
                "department": row[2],
                "description": row[3],
                "status": row[4],
                "createdAt": row[5],
                **({"purpose": row[6]} if row[6] is not None else {}),
                **({"photo": True} if row[7] else {}),
            }
            for row in rows
        ]
    except sqlite3.Error as error:
        raise RuntimeError(f"Gagal membaca database {DATABASE_FILE}: {error}") from error


notifications = load_notifications()


def save_notifications(photo=None):
    try:
        with database_connection() as connection:
            connection.execute("DELETE FROM notifications")
            connection.executemany(
                """
                INSERT INTO notifications
                    (id, customer_name, department, description, status, created_at, purpose)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["id"],
                        item.get("customerName", ""),
                        item["department"],
                        item["description"],
                        item["status"],
                        item["createdAt"],
                        item.get("purpose"),
                    )
                    for item in notifications
                ],
            )
            if photo is not None:
                notification_id, content_type, image_data = photo
                connection.execute(
                    """
                    INSERT INTO notification_photos (notification_id, content_type, image_data)
                    VALUES (?, ?, ?)
                    ON CONFLICT(notification_id) DO UPDATE SET
                        content_type = excluded.content_type,
                        image_data = excluded.image_data
                    """,
                    (notification_id, content_type, image_data),
                )
            if notifications:
                connection.execute(
                    "DELETE FROM notification_photos WHERE notification_id NOT IN "
                    "(SELECT id FROM notifications)"
                )
            else:
                connection.execute("DELETE FROM notification_photos")
    except (sqlite3.Error, RuntimeError) as error:
        raise RuntimeError(f"Gagal menyimpan data ke database: {error}") from error


def broadcast_notifications():
    with data_lock:
        listeners = tuple(subscribers.items())
        items = list(notifications)
    for subscriber, identity in listeners:
        if identity["role"] == "receiver":
            visible = [
                item for item in items
                if item.get("department") == identity["department"]
            ]
        else:
            visible = items
        subscriber.put(json.dumps(visible, ensure_ascii=False))


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "RuangLayanan/1.0"

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for header, value in (extra_headers or {}).items():
            self.send_header(header, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, maximum=16_384):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Ukuran data tidak valid.") from None
        if length < 1 or length > maximum:
            raise ValueError(f"Ukuran data harus antara 1 dan {maximum} byte.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ValueError("Format JSON tidak valid.") from error
        if not isinstance(payload, dict):
            raise ValueError("Data permintaan tidak valid.")
        return payload

    def authenticated(self):
        cookies = {
            key.strip(): value.strip()
            for part in self.headers.get("Cookie", "").split(";")
            if "=" in part
            for key, _, value in (part.strip().partition("="),)
        }
        session_id = cookies.get("ruang_layanan_session", "")
        referer_path = urlparse(self.headers.get("Referer", "")).path
        preferred_cookie = (
            "ruang_layanan_receiver_session"
            if referer_path == "/penerima"
            else "ruang_layanan_guard_session"
        )
        if not session_id:
            session_id = cookies.get(preferred_cookie, "")
        if not session_id:
            session_id = next(
                (
                    value for key, value in cookies.items()
                    if key.startswith("ruang_layanan_") and value in sessions
                ),
                "",
            )
        session = sessions.get(session_id)
        if session is None:
            return False
        if session["expiresAt"] <= time.time():
            sessions.pop(session_id, None)
            return False
        session["expiresAt"] = time.time() + 12 * 60 * 60
        return session

    def require_authentication(self, roles=None):
        identity = self.authenticated()
        if identity and (roles is None or identity["role"] in roles):
            return identity
        self.send_json(401, {"error": "Masukkan kode akses dari pengelola aplikasi."})
        return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/session":
            identity = self.authenticated()
            self.send_json(200, {
                "authenticated": bool(identity),
                "role": identity["role"] if identity else None,
                "employeeName": identity.get("employeeName") if identity else None,
                "department": identity.get("department") if identity else None,
            })
            return
        if path == "/penerima":
            identity = self.authenticated()
            if not identity or identity["role"] not in {"receiver", "manager"}:
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
        identity = None
        if path.startswith("/api/"):
            identity = self.require_authentication({"guard", "receiver", "manager"})
            if not identity:
                return
        notification_path = path.strip("/").split("/")
        if (
            len(notification_path) == 4
            and notification_path[:2] == ["api", "notifications"]
            and notification_path[3] == "photo"
        ):
            if identity["role"] not in {"receiver", "manager"}:
                self.send_json(403, {"error": "Akun ini tidak dapat membuka foto notifikasi."})
                return
            try:
                with database_connection() as connection:
                    row = connection.execute(
                        """
                        SELECT notification_photos.content_type, notification_photos.image_data,
                               notifications.department
                        FROM notification_photos
                        JOIN notifications ON notifications.id = notification_photos.notification_id
                        WHERE notification_id = ?
                        """,
                        (notification_path[2],),
                    ).fetchone()
            except (sqlite3.Error, RuntimeError) as error:
                print(error)
                self.send_json(500, {"error": "Foto gagal dibuka. Periksa database aplikasi."})
                return
            if row is None or (
                identity["role"] == "receiver"
                and row[2] != identity["department"]
            ):
                self.send_json(404, {"error": "Foto notifikasi tidak ditemukan."})
                return
            body = row[1]
            self.send_response(200)
            self.send_header("Content-Type", row[0])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/notifications":
            if identity["role"] == "guard":
                self.send_json(403, {"error": "Akun satpam hanya dapat menginput notifikasi."})
                return
            with data_lock:
                items = list(notifications)
            if identity["role"] == "receiver":
                items = [
                    item for item in items
                    if item.get("department") == identity["department"]
                ]
            self.send_json(200, items)
            return
        if path == "/api/events":
            if identity["role"] == "guard":
                self.send_json(403, {"error": "Akun satpam tidak memiliki akses daftar notifikasi."})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            subscriber = queue.Queue()
            try:
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.flush()
                with data_lock:
                    subscribers[subscriber] = identity
                    items = list(notifications)
                if identity["role"] == "receiver":
                    items = [
                        item for item in items
                        if item.get("department") == identity["department"]
                    ]
                try:
                    self.wfile.write(
                        f"data: {json.dumps(items, ensure_ascii=False)}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                while True:
                    try:
                        message = subscriber.get(timeout=20)
                        self.wfile.write(f"data: {message}\n\n".encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with data_lock:
                    subscribers.pop(subscriber, None)
            return
        if path in {"/", "/index.html"}:
            try:
                body = (ROOT / "index.html").read_bytes()
            except OSError as error:
                self.send_json(500, {"error": f"Gagal membuka aplikasi: {error}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/manifest.webmanifest":
            try:
                body = (ROOT / "manifest.webmanifest").read_bytes()
            except OSError as error:
                self.send_json(500, {"error": f"Gagal membuka manifest aplikasi: {error}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if path in {"/service-worker.js", "/install.js"}:
            try:
                body = (ROOT / path.lstrip("/")).read_bytes()
            except OSError as error:
                self.send_json(500, {"error": f"Gagal membuka komponen aplikasi: {error}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            if path == "/service-worker.js":
                self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/penerima":
            try:
                body = (ROOT / "penerima.html").read_bytes()
            except OSError as error:
                self.send_json(500, {"error": f"Gagal membuka halaman penerima: {error}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        asset_files = {
            "/assets/logo-danantara-v2.svg": ("logo-danantara-v2.svg", "image/svg+xml"),
            "/assets/logo-mandiri-taspen.png": ("logo-mandiri-taspen.png", "image/png"),
            "/assets/taspen-symbol.png": ("taspen-symbol.png", "image/png"),
            "/assets/taspen-service-background.png": ("taspen-service-background.png", "image/png"),
            "/assets/mandiri-taspen-icon-180.png": ("mandiri-taspen-icon-180.png", "image/png"),
            "/assets/mandiri-taspen-icon-192.png": ("mandiri-taspen-icon-192.png", "image/png"),
            "/assets/mandiri-taspen-icon-512.png": ("mandiri-taspen-icon-512.png", "image/png"),
            "/assets/mandiri-taspen-icon-maskable-512.png": ("mandiri-taspen-icon-maskable-512.png", "image/png"),
        }
        if path in asset_files:
            filename, content_type = asset_files[path]
            try:
                body = (ROOT / "assets" / filename).read_bytes()
            except OSError as error:
                self.send_json(500, {"error": f"Gagal membuka aset aplikasi: {error}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json(404, {"error": "Halaman tidak ditemukan."})

    def do_POST(self):
        path = urlparse(self.path).path
        if path in {"/api/access/login", "/api/login", "/api/receiver/login"}:
            try:
                payload = self.read_json()
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
                return
            submitted_code = payload.get("code", "")
            if isinstance(submitted_code, str):
                submitted_code = submitted_code.strip().upper()
            employee_name = None
            department = None
            is_receiver = path == "/api/receiver/login"
            if path == "/api/access/login":
                if isinstance(submitted_code, str) and secrets.compare_digest(submitted_code, guard_access_code.upper()):
                    is_receiver = False
                else:
                    for unit, unit_code in receiver_access_codes.items():
                        if isinstance(submitted_code, str) and secrets.compare_digest(submitted_code, unit_code.upper()):
                            is_receiver = True
                            department = unit
                            employee_name = "KCP" if unit == "KCP" else "Pegawai"
                            break
                    if not is_receiver:
                        self.send_json(401, {"error": "Kode akses tidak dikenal. Periksa kembali kode dari pengelola."})
                        return
            if is_receiver:
                if path != "/api/access/login":
                    employee_name = payload.get("employeeName")
                    if not isinstance(employee_name, str) or not employee_name.strip() or len(employee_name) > 100:
                        self.send_json(400, {"error": "Masukkan nama pegawai yang menerima notifikasi."})
                        return
                    employee_name = employee_name.strip()
                    department = payload.get("department")
                    if not isinstance(department, str) or department not in DEPARTMENTS:
                        self.send_json(400, {"error": "Pilih unit kerja yang sesuai."})
                        return
                code = receiver_access_codes[department]
            elif path != "/api/access/login":
                code = guard_access_code
            if path != "/api/access/login" and (
                not isinstance(submitted_code, str)
                or not secrets.compare_digest(submitted_code, code.upper())
            ):
                self.send_json(401, {"error": "Kode akses salah untuk unit kerja yang dipilih." if is_receiver else "Kode akses salah. Periksa kode yang ditampilkan di komputer server."})
                return
            session_id = secrets.token_urlsafe(32)
            sessions[session_id] = {
                "role": "receiver" if is_receiver else "guard",
                "employeeName": employee_name,
                "department": department,
                "expiresAt": time.time() + 12 * 60 * 60,
            }
            if is_receiver and department == "KCP" and path == "/api/access/login":
                sessions[session_id]["role"] = "manager"
            cookie_name = "ruang_layanan_session"
            secure_attribute = "; Secure" if secure_cookies else ""
            self.send_json(
                200,
                {
                    "authenticated": True,
                    "role": sessions[session_id]["role"],
                    "employeeName": employee_name,
                    "department": department,
                },
                {
                    "Set-Cookie": (
                        f"{cookie_name}={session_id}; HttpOnly; SameSite=Strict; "
                        f"Path=/; Max-Age=43200{secure_attribute}"
                    )
                },
            )
            return
        if path == "/api/notifications/bulk-delete":
            identity = self.require_authentication({"receiver", "manager"})
            if not identity:
                return
            try:
                payload = self.read_json(maximum=MAX_BULK_DELETE_REQUEST_BYTES)
                notification_ids = payload.get("ids")
                if (
                    not isinstance(notification_ids, list)
                    or not notification_ids
                    or any(not isinstance(item_id, str) or not item_id for item_id in notification_ids)
                    or len(notification_ids) != len(set(notification_ids))
                ):
                    raise ValueError("Pilih minimal satu riwayat yang akan dihapus.")
                selected_ids = set(notification_ids)
                with data_lock:
                    selected = [
                        item for item in notifications
                        if item["id"] in selected_ids
                    ]
                    if len(selected) != len(selected_ids):
                        self.send_json(404, {"error": "Sebagian riwayat tidak ditemukan atau sudah dihapus."})
                        return
                    if any(item.get("status") != "done" for item in selected):
                        self.send_json(403, {"error": "Penghapusan massal hanya berlaku untuk riwayat yang sudah selesai."})
                        return
                    if identity["role"] == "receiver" and any(
                        item.get("department") != identity["department"]
                        for item in selected
                    ):
                        self.send_json(403, {"error": "Pegawai hanya dapat menghapus riwayat notifikasi unitnya."})
                        return
                    previous_notifications = list(notifications)
                    notifications[:] = [
                        item for item in notifications
                        if item["id"] not in selected_ids
                    ]
                    try:
                        save_notifications()
                    except RuntimeError:
                        notifications[:] = previous_notifications
                        raise
                broadcast_notifications()
                self.send_json(
                    200,
                    {"deletedCount": len(selected), "deletedIds": notification_ids},
                )
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            except RuntimeError as error:
                print(error)
                self.send_json(500, {"error": "Penghapusan riwayat gagal disimpan. Periksa ruang disk dan izin folder."})
            return
        if path == "/api/logout":
            identity = self.require_authentication({"guard", "receiver", "manager"})
            if not identity:
                return
            session_id = next(
                (
                    key for key, session in sessions.items()
                    if session is identity
                ),
                None,
            )
            if session_id is None:
                self.send_json(401, {"error": "Sesi login sudah berakhir."})
                return
            sessions.pop(session_id, None)
            cookie_name = "ruang_layanan_session"
            secure_attribute = "; Secure" if secure_cookies else ""
            self.send_json(
                200,
                {"authenticated": False},
                {
                    "Set-Cookie": (
                        f"{cookie_name}=; HttpOnly; SameSite=Strict; Path=/; "
                        "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
                        f"{secure_attribute}"
                    )
                },
            )
            return
        if path != "/api/notifications":
            self.send_json(404, {"error": "Alamat tidak ditemukan."})
            return
        if not self.require_authentication({"guard", "manager"}):
            return
        try:
            payload = self.read_json(maximum=MAX_NOTIFICATION_REQUEST_BYTES)
            required = ("department", "description")
            if any(not isinstance(payload.get(field), str) or not payload[field].strip() for field in required):
                raise ValueError("Tujuan dan keterangan wajib diisi.")
            if payload["department"] not in DEPARTMENTS:
                raise ValueError("Tujuan tidak dikenal.")
            customer_name = validate_customer_name(payload.get("customerName", ""))
            photo = validate_notification_photo(payload.get("photo"))
            for field, maximum in (("description", 200),):
                if len(str(payload.get(field, ""))) > maximum:
                    raise ValueError(f"{field} melebihi batas {maximum} karakter.")
            item = {
                "id": str(uuid.uuid4()),
                "customerName": customer_name,
                "department": payload["department"],
                "description": payload["description"].strip(),
                "status": "active",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                **({"photo": True} if photo else {}),
            }
            with data_lock:
                notifications.insert(0, item)
                try:
                    save_notifications(
                        (item["id"], photo[0], photo[1]) if photo else None
                    )
                except RuntimeError:
                    notifications.pop(0)
                    raise
            broadcast_notifications()
            self.send_json(201, item)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except RuntimeError as error:
            print(error)
            self.send_json(500, {"error": "Notifikasi gagal disimpan. Periksa ruang disk dan izin folder."})

    def do_PATCH(self):
        identity = self.require_authentication({"receiver", "manager"})
        if not identity:
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "notifications"]:
            self.send_json(404, {"error": "Alamat tidak ditemukan."})
            return
        try:
            payload = self.read_json()
            if payload.get("status") not in {"active", "done"}:
                raise ValueError("Status notifikasi tidak valid.")
            if identity["role"] == "receiver" and payload["status"] != "done":
                self.send_json(403, {"error": "Pegawai hanya dapat menandai notifikasi unitnya selesai."})
                return
            with data_lock:
                item = next((entry for entry in notifications if entry["id"] == parts[2]), None)
                if item is None:
                    self.send_json(404, {"error": "Notifikasi tidak ditemukan."})
                    return
                if identity["role"] == "receiver" and item.get("department") != identity["department"]:
                    self.send_json(403, {"error": "Pegawai hanya dapat memperbarui notifikasi unitnya."})
                    return
                old_status = item["status"]
                item["status"] = payload["status"]
                try:
                    save_notifications()
                except RuntimeError:
                    item["status"] = old_status
                    raise
            broadcast_notifications()
            self.send_json(200, item)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except RuntimeError as error:
            print(error)
            self.send_json(500, {"error": "Perubahan gagal disimpan. Periksa ruang disk dan izin folder."})

    def do_DELETE(self):
        identity = self.require_authentication({"receiver", "manager"})
        if not identity:
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "notifications"]:
            self.send_json(404, {"error": "Alamat tidak ditemukan."})
            return
        with data_lock:
            index = next((position for position, item in enumerate(notifications) if item["id"] == parts[2]), None)
            if index is None:
                self.send_json(404, {"error": "Notifikasi tidak ditemukan."})
                return
            item = notifications[index]
            if identity["role"] == "receiver":
                if item.get("department") != identity["department"]:
                    self.send_json(403, {"error": "Pegawai hanya dapat menghapus riwayat notifikasi unitnya."})
                    return
                if item.get("status") != "done":
                    self.send_json(403, {"error": "Pegawai hanya dapat menghapus notifikasi yang sudah selesai."})
                    return
            item = notifications.pop(index)
            try:
                save_notifications()
            except RuntimeError as error:
                notifications.insert(index, item)
                print(error)
                self.send_json(500, {"error": "Penghapusan gagal disimpan. Periksa ruang disk dan izin folder."})
                return
        broadcast_notifications()
        self.send_json(200, {"deleted": True})


def main():
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    server.daemon_threads = True
    print(f"Layanan Mandiri Taspen berjalan pada port {PORT}.")
    if public_deployment:
        print(
            "Mode hosting publik aktif; pastikan HTTPS dan disk persisten "
            "dikonfigurasi oleh penyedia hosting."
        )
        print("Kode akses tidak dicetak pada mode hosting publik.")
        print("Tekan Ctrl+C untuk menghentikan server.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer dihentikan.")
        finally:
            server.server_close()
        return

    print("Di komputer server: http://localhost:{0}".format(PORT))
    if ipaddress.ip_address(HOST).is_loopback:
        print(f"Aplikasi hanya dapat dibuka di komputer ini: http://127.0.0.1:{PORT}")
    else:
        addresses = sorted({
            address[4][0]
            for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            if not ipaddress.ip_address(address[4][0]).is_loopback
        })
        if addresses:
            for address in addresses:
                print(f"Pegawai lain: http://{address}:{PORT}")
                print(f"Halaman penerima: http://{address}:{PORT}/penerima")
        else:
            print("Alamat jaringan kantor belum terdeteksi. Periksa koneksi jaringan komputer server.")
        print("Pastikan komputer server dan perangkat pegawai berada di jaringan kantor yang sama.")
    print(f"Kode akses pengirim/satpam: {guard_access_code}")
    print(f"Kode akses penerima per unit tercatat di {RECEIVER_CODES_FILE}.")
    print("Bagikan kepada pegawai hanya kode untuk unit kerja masing-masing.")
    print("Kode akses pengirim bawaan mudah diingat; ganti sebelum penggunaan operasional.")
    print("Peringatan: koneksi masih HTTP tanpa enkripsi. Gunakan hanya untuk uji coba dengan data contoh.")
    print("Tekan Ctrl+C untuk menghentikan server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer dihentikan.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
