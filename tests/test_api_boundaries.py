"""Backend boundary tests: all external DB/Redis calls are mocked."""
import unittest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException
import main


class TestSkyCareAPI(unittest.TestCase):
    def test_health_is_only_basic_response(self):
        self.assertEqual(main.health(), {"status": "ok"})

    def test_alternatives_fall_back_when_database_is_unavailable(self):
        with patch("main.get_db_connection", side_effect=RuntimeError("db unavailable")):
            self.assertEqual(main.get_alternatives(), main.FALLBACK_ALTERNATIVES)

    def test_queue_registration_returns_queued_not_booked(self):
        redis_client = MagicMock()
        request = main.ReservationRequest(
            flight_id="LCC912", passenger_name="홍길동",
            passenger_phone="010-0000-0000",
        )
        with patch("main.get_redis_client", return_value=redis_client):
            result = main.queue_reservation(request)
        self.assertEqual(result["status"], "queued")
        self.assertTrue(result["request_id"].startswith("REQ-"))
        redis_client.lpush.assert_called_once()
        self.assertEqual(redis_client.lpush.call_args.args[0], main.QUEUE_NAME)

    def test_queue_registration_failure_returns_500(self):
        request = main.ReservationRequest(
            flight_id="LCC912", passenger_name="홍길동",
            passenger_phone="010-0000-0000",
        )
        with patch("main.get_redis_client", side_effect=ConnectionError("redis down")):
            with self.assertRaises(HTTPException) as caught:
                main.queue_reservation(request)
        self.assertEqual(caught.exception.status_code, 500)

    def test_queue_status_returns_lengths_only(self):
        redis_client = MagicMock()
        redis_client.llen.side_effect = [3, 1]
        with patch("main.get_redis_client", return_value=redis_client):
            result = main.queue_status()
        self.assertEqual(result, {
            "queue_name": main.QUEUE_NAME, "queue_length": 3,
            "dlq_name": main.DLQ_NAME, "dlq_length": 1,
        })

    def test_queue_status_failure_returns_500(self):
        with patch("main.get_redis_client", side_effect=ConnectionError("redis down")):
            with self.assertRaises(HTTPException) as caught:
                main.queue_status()
        self.assertEqual(caught.exception.status_code, 500)

    def test_reservations_failure_returns_500(self):
        with patch("main.get_db_connection", side_effect=RuntimeError("db unavailable")):
            with self.assertRaises(HTTPException) as caught:
                main.list_reservations()
        self.assertEqual(caught.exception.status_code, 500)

    def test_reservations_are_private_by_default(self):
        with patch.dict(main.os.environ, {"SKYCARE_ADMIN_API_KEY": ""}):
            for supplied_key in (None, "guess"):
                with self.assertRaises(HTTPException) as caught:
                    main.require_admin_key(supplied_key)
                self.assertEqual(caught.exception.status_code, 403)

    def test_reservations_require_matching_admin_key(self):
        with patch.dict(main.os.environ, {"SKYCARE_ADMIN_API_KEY": "demo-test-secret"}):
            with self.assertRaises(HTTPException) as caught:
                main.require_admin_key("wrong")
            self.assertEqual(caught.exception.status_code, 403)
            self.assertIsNone(main.require_admin_key("demo-test-secret"))
        route = next(route for route in main.app.routes if getattr(route, "path", "") == "/reservations")
        self.assertTrue(any(dep.call is main.require_admin_key for dep in route.dependant.dependencies))

    def test_both_demo_pages_require_phone_suffix_for_status_lookup(self):
        from pathlib import Path
        pages = [main.home(), (Path(main.__file__).parent / "docs" / "index.html").read_text(encoding="utf-8")]
        for page in pages:
            self.assertIn("matchesDemoIdentity(state.statusResNo, state.statusPhone)", page)
            self.assertIn("f.phoneLast4 === String(phoneLast4).trim()", page)


if __name__ == "__main__":
    unittest.main()
