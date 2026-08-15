import os
import unittest

import nomad_web


class AuthFlowTests(unittest.TestCase):
    def setUp(self):
        os.environ["NOMAD_USERNAME"] = "admin"
        os.environ["NOMAD_PASSWORD"] = "nomad2026"
        nomad_web.app.config.update(TESTING=True)
        self.client = nomad_web.app.test_client()

    def test_login_accepts_valid_credentials(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])

    def test_login_rejects_invalid_credentials(self):
        response = self.client.post(
            "/api/auth/login",
            json={"username": "bad", "password": "wrong"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["ok"])

    def test_login_without_json_body_returns_validation_error(self):
        response = self.client.post("/api/auth/login")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_tunnel_health_check_returns_loss_data(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.post("/api/tunnel/health_check")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertIn("loss", response.get_json())

    def test_media_open_folder_returns_path_for_existing_directory(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.post(
            "/api/media/open_folder",
            json={"path": os.path.join(nomad_web.BASE_DIR, "downloads")},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        self.assertIn("downloads", response.get_json()["path"])

    def test_media_intelligence_returns_queue_and_tool_summary(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.get("/api/media/intelligence")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("engines", data)
        self.assertIn("queue_summary", data)
        self.assertIn("recommendations", data)
        self.assertIsInstance(data["recommendations"], list)

    def test_intelligence_overview_returns_dashboard_sections(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.get("/api/intelligence/overview")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("audio", data)
        self.assertIn("charts", data)
        self.assertIn("playlist", data)
        self.assertIn("memory", data)

    def test_audio_analysis_endpoint_returns_summary(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.get("/api/audio/analyze")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("summary", data)

    def test_charts_endpoint_returns_chart_entries(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.get("/api/charts/apple")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("tracks", data)

    def test_lyrics_search_endpoint_returns_results(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.get("/api/lyrics/search", query_string={"q": "hello"})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("results", data)

    def test_local_ai_playlist_endpoint_returns_plan(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.post(
            "/api/ai/local/compose",
            json={"prompt": "rainy day chill", "count": 6},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("playlist", data)

    def test_events_endpoint_streams_hello_message(self):
        self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "nomad2026"},
        )
        response = self.client.get("/events", buffered=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.content_type)
        body = next(response.response).decode("utf-8")
        self.assertIn("hello", body)


if __name__ == "__main__":
    unittest.main()
