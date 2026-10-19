import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server


PNG_DATA = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_DATA_URL = f"data:image/png;base64,{base64.b64encode(PNG_DATA).decode()}"


class NotificationPhotoValidationTests(unittest.TestCase):
    def test_accepts_png_photo_data(self):
        self.assertEqual(server.validate_notification_photo(PNG_DATA_URL), ("image/png", PNG_DATA))

    def test_photo_is_optional(self):
        self.assertIsNone(server.validate_notification_photo(None))
        self.assertIsNone(server.validate_notification_photo(""))

    def test_rejects_unsupported_format_and_mismatched_content(self):
        for data_url in ("data:image/svg+xml;base64,PHN2Zz4=", "data:image/jpeg;base64,Zm9v"):
            with self.subTest(data_url=data_url), self.assertRaises(ValueError):
                server.validate_notification_photo(data_url)

    def test_rejects_invalid_base64_and_photos_over_five_megabytes(self):
        for data_url in (
            "data:image/png;base64,not base64!",
            "data:image/png;base64," + base64.b64encode(
                b"\x89PNG\r\n\x1a\n" + b"x" * server.MAX_NOTIFICATION_PHOTO_BYTES
            ).decode(),
        ):
            with self.subTest(length=len(data_url)), self.assertRaises(ValueError):
                server.validate_notification_photo(data_url)


class NotificationPhotoApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RequestHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        database_file = Path(self.temp_directory.name) / "notifications.db"
        legacy_file = Path(self.temp_directory.name) / "missing-notifications.json"
        self.notifications = []
        self.session_id = "notification-photo-test-session"
        self.patches = [
            patch.object(server, "DATABASE_FILE", database_file),
            patch.object(server, "LEGACY_DATA_FILE", legacy_file),
            patch.object(server, "notifications", self.notifications),
            patch.dict(server.sessions, {
                self.session_id: {
                    "role": "manager",
                    "expiresAt": time.time() + 60,
                }
            }),
            patch.object(server, "broadcast_notifications"),
        ]
        for active_patch in self.patches:
            active_patch.start()
        server.initialize_database()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_directory.cleanup()

    def request(self, method, path, payload=None, session_id=None):
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Cookie": f"ruang_layanan_session={session_id or self.session_id}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        result = (
            json.loads(response_body)
            if response.getheader("Content-Type", "").startswith("application/json")
            else response_body
        )
        content_type = response.getheader("Content-Type")
        connection.close()
        return response.status, content_type, result

    def test_photo_is_saved_and_served_to_its_unit(self):
        status, _, item = self.request("POST", "/api/notifications", {
            "customerName": "Siti",
            "department": "CS",
            "description": "Pemeriksaan dokumen",
            "photo": PNG_DATA_URL,
        })
        self.assertEqual(status, 201)
        self.assertTrue(item["photo"])
        self.assertTrue(server.load_notifications()[0]["photo"])

        status, content_type, image_data = self.request(
            "GET", f"/api/notifications/{item['id']}/photo"
        )
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(image_data, PNG_DATA)

        server.sessions[self.session_id]["role"] = "receiver"
        server.sessions[self.session_id]["department"] = "TELLER"
        status, _, _ = self.request(
            "GET", f"/api/notifications/{item['id']}/photo"
        )
        self.assertEqual(status, 404)

    def test_deleting_notification_also_deletes_its_photo(self):
        status, _, item = self.request("POST", "/api/notifications", {
            "customerName": "Siti",
            "department": "CS",
            "description": "Pemeriksaan dokumen",
            "photo": PNG_DATA_URL,
        })
        self.assertEqual(status, 201)

        status, _, result = self.request(
            "DELETE", f"/api/notifications/{item['id']}"
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["deleted"])
        status, _, _ = self.request(
            "GET", f"/api/notifications/{item['id']}/photo"
        )
        self.assertEqual(status, 404)

    def test_receiver_can_delete_completed_history_in_own_department(self):
        self.notifications.append({
            "id": "completed-cs",
            "customerName": "Siti",
            "department": "CS",
            "description": "Pemeriksaan dokumen",
            "status": "done",
            "createdAt": "2026-10-19T00:00:00+00:00",
        })
        server.save_notifications()
        server.sessions[self.session_id].update({
            "role": "receiver",
            "department": "CS",
        })

        status, _, result = self.request(
            "DELETE", "/api/notifications/completed-cs"
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["deleted"])
        self.assertEqual(self.notifications, [])

    def test_receiver_cannot_delete_active_or_other_department_notification(self):
        self.notifications.extend([
            {
                "id": "active-cs",
                "customerName": "Siti",
                "department": "CS",
                "description": "Pemeriksaan dokumen",
                "status": "active",
                "createdAt": "2026-10-19T00:00:00+00:00",
            },
            {
                "id": "completed-teller",
                "customerName": "Budi",
                "department": "TELLER",
                "description": "Pemeriksaan dokumen",
                "status": "done",
                "createdAt": "2026-10-19T00:00:00+00:00",
            },
        ])
        server.save_notifications()
        server.sessions[self.session_id].update({
            "role": "receiver",
            "department": "CS",
        })

        for notification_id in ("active-cs", "completed-teller"):
            with self.subTest(notification_id=notification_id):
                status, _, result = self.request(
                    "DELETE", f"/api/notifications/{notification_id}"
                )
                self.assertEqual(status, 403)
                self.assertIn("error", result)
        self.assertEqual(len(self.notifications), 2)

    def test_bulk_delete_removes_only_selected_completed_history(self):
        self.notifications.extend([
            {
                "id": "completed-cs-1",
                "customerName": "Siti",
                "department": "CS",
                "description": "Pemeriksaan dokumen",
                "status": "done",
                "createdAt": "2026-10-19T00:00:00+00:00",
            },
            {
                "id": "completed-cs-2",
                "customerName": "Dina",
                "department": "CS",
                "description": "Penggantian kartu",
                "status": "done",
                "createdAt": "2026-10-19T01:00:00+00:00",
            },
            {
                "id": "active-cs",
                "customerName": "Budi",
                "department": "CS",
                "description": "Permintaan aktif",
                "status": "active",
                "createdAt": "2026-10-19T02:00:00+00:00",
            },
        ])
        server.save_notifications()
        server.sessions[self.session_id].update({
            "role": "receiver",
            "department": "CS",
        })

        status, _, result = self.request(
            "POST",
            "/api/notifications/bulk-delete",
            {"ids": ["completed-cs-1", "completed-cs-2"]},
        )

        self.assertEqual(status, 200)
        self.assertEqual(result["deletedCount"], 2)
        self.assertEqual(result["deletedIds"], ["completed-cs-1", "completed-cs-2"])
        self.assertEqual([item["id"] for item in self.notifications], ["active-cs"])

    def test_bulk_delete_rejects_active_other_unit_and_missing_ids_without_partial_deletion(self):
        self.notifications.extend([
            {
                "id": "completed-cs",
                "customerName": "Siti",
                "department": "CS",
                "description": "Riwayat unit CS",
                "status": "done",
                "createdAt": "2026-10-19T00:00:00+00:00",
            },
            {
                "id": "completed-teller",
                "customerName": "Budi",
                "department": "TELLER",
                "description": "Riwayat unit TELLER",
                "status": "done",
                "createdAt": "2026-10-19T01:00:00+00:00",
            },
            {
                "id": "active-cs",
                "customerName": "Dina",
                "department": "CS",
                "description": "Permintaan aktif",
                "status": "active",
                "createdAt": "2026-10-19T02:00:00+00:00",
            },
        ])
        server.save_notifications()
        server.sessions[self.session_id].update({
            "role": "receiver",
            "department": "CS",
        })

        for notification_ids, expected_status in (
            (["completed-cs", "active-cs"], 403),
            (["completed-cs", "completed-teller"], 403),
            (["completed-cs", "missing-id"], 404),
        ):
            with self.subTest(notification_ids=notification_ids):
                status, _, result = self.request(
                    "POST",
                    "/api/notifications/bulk-delete",
                    {"ids": notification_ids},
                )
                self.assertEqual(status, expected_status)
                self.assertIn("error", result)
                self.assertEqual(len(self.notifications), 3)

    def test_bulk_delete_rejects_empty_and_duplicate_selections(self):
        for notification_ids in ([], ["same-id", "same-id"]):
            with self.subTest(notification_ids=notification_ids):
                status, _, result = self.request(
                    "POST",
                    "/api/notifications/bulk-delete",
                    {"ids": notification_ids},
                )
                self.assertEqual(status, 400)
                self.assertIn("error", result)

    def test_photo_can_be_omitted(self):
        status, _, item = self.request("POST", "/api/notifications", {
            "customerName": "Siti",
            "department": "CS",
            "description": "Pemeriksaan dokumen",
        })
        self.assertEqual(status, 201)
        self.assertNotIn("photo", item)

    def test_api_rejects_photo_with_invalid_signature(self):
        status, _, result = self.request("POST", "/api/notifications", {
            "customerName": "Siti",
            "department": "CS",
            "description": "Pemeriksaan dokumen",
            "photo": "data:image/png;base64," + base64.b64encode(b"not a png").decode(),
        })
        self.assertEqual(status, 400)
        self.assertIn("format", result["error"])
        self.assertEqual(self.notifications, [])


if __name__ == "__main__":
    unittest.main()
