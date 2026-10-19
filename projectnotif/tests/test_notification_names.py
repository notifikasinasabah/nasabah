import http.client
import json
import threading
import time
import unittest
from unittest.mock import patch

import server


class CustomerNameValidationTests(unittest.TestCase):
    def test_accepts_letters_spaces_and_accented_letters(self):
        self.assertEqual(server.validate_customer_name(" Budi Santoso "), "Budi Santoso")
        self.assertEqual(server.validate_customer_name("José"), "José")
        self.assertEqual(server.validate_customer_name("Jose\u0301"), "Jose\u0301")
        self.assertEqual(server.validate_customer_name(""), "")

    def test_rejects_digits_punctuation_and_non_space_whitespace(self):
        for name in ("Budi2", "Budi-Santoso", "Budi_Santoso", "Budi\nSantoso"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                server.validate_customer_name(name)

    def test_rejects_non_string_and_names_longer_than_200_characters(self):
        for name in (123, "A" * 201):
            with self.subTest(name=name), self.assertRaises(ValueError):
                server.validate_customer_name(name)


class NotificationNameApiTests(unittest.TestCase):
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
        self.notifications = []
        self.session_id = "customer-name-test-session"
        self.patches = [
            patch.object(server, "notifications", self.notifications),
            patch.dict(server.sessions, {
                self.session_id: {
                    "role": "manager",
                    "expiresAt": time.time() + 60,
                }
            }),
            patch.object(server, "save_notifications"),
            patch.object(server, "broadcast_notifications"),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()

    def create_notification(self, customer_name):
        body = json.dumps({
            "customerName": customer_name,
            "department": "CS",
            "description": "Bantuan",
        }).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            "/api/notifications",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Cookie": f"ruang_layanan_session={self.session_id}",
            },
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_api_accepts_letter_names_and_rejects_invalid_names(self):
        status, created = self.create_notification("Siti Nur Aisyah")
        self.assertEqual(status, 201)
        self.assertEqual(created["customerName"], "Siti Nur Aisyah")

        for name in ("Nasabah 123", "Nama-Sambung", "Nama."):
            with self.subTest(name=name):
                status, error = self.create_notification(name)
                self.assertEqual(status, 400)
                self.assertIn("Nama nasabah", error["error"])

        self.assertEqual(len(self.notifications), 1)


if __name__ == "__main__":
    unittest.main()
