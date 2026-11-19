"""Queue client in hexio_setup.py, against fake HTTP responses.

Response bodies match the cyberx-event-mgmt license queue API (v1.7.8+).
Run: python3 -m unittest discover -s tests
"""
import io
import json
import sys
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

# hexio_setup imports pexpect at module load (and pip-installs it if missing)
# and uses pexpect.spawn in annotations; a stub is enough for the queue client.
_pexpect_stub = types.ModuleType("pexpect")
_pexpect_stub.spawn = type("spawn", (), {})
sys.modules.setdefault("pexpect", _pexpect_stub)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hexio_setup  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok(body):
    return FakeResponse(json.dumps(body).encode())


def http_error(code):
    return urllib.error.HTTPError("https://q/acquire", code, "err", {}, io.BytesIO(b"{}"))


class AcquireQueueSlot(unittest.TestCase):

    def run_acquire(self, *responses):
        calls = []

        def urlopen(req, context=None, timeout=None):
            calls.append(req)
            item = responses[len(calls) - 1]
            if isinstance(item, BaseException):
                raise item
            return item

        with mock.patch.object(hexio_setup.urllib.request, "urlopen", urlopen), \
             mock.patch.object(hexio_setup.time, "sleep") as sleep:
            result = hexio_setup.acquire_queue_slot("https://q/", "tok-123", "ts-01",
                                                    poll_interval=7)
        return result, calls, sleep

    def test_waits_while_full_then_returns_the_granted_slot(self):
        slot, calls, sleep = self.run_acquire(
            ok({"status": "wait", "active": 2, "max": 2, "retry_after": 30}),
            ok({"status": "granted", "slot_id": "abcdefgh1234", "active": 2, "max": 2}),
        )
        self.assertEqual(slot, "abcdefgh1234")
        sleep.assert_called_once_with(30)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer tok-123")
        self.assertEqual(json.loads(calls[0].data), {"hostname": "ts-01"})

    def test_rejected_token_raises_without_retrying(self):
        for code in (401, 403):
            with self.subTest(code=code):
                with self.assertRaises(hexio_setup.QueueAuthError):
                    self.run_acquire(http_error(code))

    def test_server_errors_and_network_failures_are_retried(self):
        slot, calls, sleep = self.run_acquire(
            http_error(503),
            urllib.error.URLError("connection refused"),
            TimeoutError("timed out"),
            ok({"status": "granted", "slot_id": "slot-0001", "active": 1, "max": 2}),
        )
        self.assertEqual(slot, "slot-0001")
        self.assertEqual(len(calls), 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [7, 7, 7])

    def test_other_client_errors_are_not_retried(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.run_acquire(http_error(422))

    def test_tolerates_responses_without_occupancy_fields(self):
        # Servers before v1.7.8 omit active/max/retry_after.
        slot, _, sleep = self.run_acquire(
            ok({"status": "wait"}),
            ok({"status": "granted", "slot_id": "slot-0002"}),
        )
        self.assertEqual(slot, "slot-0002")
        sleep.assert_called_once_with(7)   # falls back to poll_interval


class ReleaseQueueSlot(unittest.TestCase):

    def test_sends_elapsed_seconds_and_tolerates_missing_fields(self):
        sent = []

        def urlopen(req, context=None, timeout=None):
            sent.append(req)
            return ok({"status": "success", "message": "Slot released"})

        with mock.patch.object(hexio_setup.urllib.request, "urlopen", urlopen):
            hexio_setup.release_queue_slot("https://q", "tok", "slot-1", "success", 321)

        self.assertEqual(json.loads(sent[0].data),
                         {"slot_id": "slot-1", "result": "success", "elapsed_seconds": 321})
        self.assertTrue(sent[0].full_url.endswith("/release"))

    def test_failure_is_logged_not_raised(self):
        with mock.patch.object(hexio_setup.urllib.request, "urlopen",
                               side_effect=http_error(500)), \
             self.assertLogs(hexio_setup.log, level="WARNING"):
            hexio_setup.release_queue_slot("https://q", "tok", "slot-1", "error", 5)


if __name__ == "__main__":
    unittest.main()
