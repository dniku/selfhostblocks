"""Exercise the generated restore hook against a synthetic HTTP API."""

import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import threading
import unittest
from http import HTTPMethod, HTTPStatus, server

_ROOT = pathlib.Path("/tmp/agent/codex/immich-restore-test")
_DUMP = _ROOT / "backups" / "test-only-dump.sql.gz"
_CONTENTS = b"test-only recovery data"


@dataclasses.dataclass(frozen=True)
class _Reply:
    method: HTTPMethod
    status: HTTPStatus = HTTPStatus.OK
    body: bytes = b"{}"
    outage: bool = False


_START = _Reply(HTTPMethod.POST)
_ACTIVE = _Reply(HTTPMethod.GET, body=b'{"active":true,"action":"restore_database"}')
_END = _Reply(HTTPMethod.GET, body=b'{"active":false,"action":"end"}')
_DELETE = _Reply(HTTPMethod.DELETE)


class _RestoreTest(unittest.TestCase):
    def _run(
        self, replies: list[_Reply], *, success: bool, deleted: bool
    ) -> tuple[str, list[HTTPMethod]]:
        requests: list[HTTPMethod] = []
        errors: list[str] = []
        ready = threading.Event()
        stop = threading.Event()
        restart = threading.Event()

        class Handler(server.BaseHTTPRequestHandler):
            def _respond(self) -> None:
                method = HTTPMethod(self.command)
                requests.append(method)
                # Repeat the last response to exercise real timeout behavior.
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if method != reply.method:
                    errors.append(f"Expected {reply.method}, received {method}")
                expected_path = {
                    HTTPMethod.POST: "/api/admin/maintenance",
                    HTTPMethod.GET: "/api/admin/maintenance/status",
                    HTTPMethod.DELETE: "/api/admin/database-backups",
                }[method]
                if self.path != expected_path:
                    errors.append(f"Unexpected API path: {self.path}")
                if self.headers["x-api-key"] != "test-only-api-key":
                    errors.append("Missing test API key")
                if method != HTTPMethod.GET:
                    payload = json.loads(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    expected_payload = (
                        {
                            "action": "restore_database",
                            "restoreBackupFilename": _DUMP.name,
                        }
                        if method == HTTPMethod.POST
                        else {"backups": [_DUMP.name]}
                    )
                    if payload != expected_payload:
                        errors.append("Unexpected request payload")
                if method == HTTPMethod.DELETE and reply.status == HTTPStatus.OK:
                    _DUMP.unlink(missing_ok=True)
                if reply.outage:
                    # Close the listener before replying: the next request must
                    # cross a restart gap, rather than racing a later shutdown.
                    self.server.server_close()
                    restart.set()
                self.send_response(reply.status)
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

            do_POST = _respond
            do_GET = _respond
            do_DELETE = _respond

            def log_message(self, format: str, *args: object) -> None:
                pass

        def serve() -> None:
            while not stop.is_set():
                with server.HTTPServer(("127.0.0.1", 2283), Handler) as api:
                    api.timeout = 0.1
                    ready.set()
                    while not stop.is_set() and not restart.is_set():
                        api.handle_request()
                if restart.is_set():
                    stop.wait(3)
                    restart.clear()

        with contextlib.ExitStack() as stack:
            _DUMP.parent.mkdir(parents=True, exist_ok=True)
            stack.callback(shutil.rmtree, _ROOT)
            _DUMP.write_bytes(_CONTENTS)
            (_DUMP.parent / ".latest").write_text(_DUMP.name)
            (_ROOT / "api-key").write_text("test-only-api-key")
            worker = threading.Thread(target=serve)
            worker.start()
            stack.callback(worker.join)
            stack.callback(stop.set)
            self.assertTrue(ready.wait(5), "Test API did not start")
            result = subprocess.run(
                [os.environ["IMMICH_RESTORE_HOOK"]],
                check=False,
                capture_output=True,
                text=True,
                timeout=140,  # Includes the real two-minute cleanup budget.
            )
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode == 0, success, output)
            self.assertEqual(_DUMP.exists(), not deleted, output)
            if not deleted:
                self.assertEqual(_DUMP.read_bytes(), _CONTENTS)
            self.assertEqual(errors, [], output)
            self.assertEqual(requests.count(HTTPMethod.POST), 1, output)
        return output, requests

    def test_restore_error_preserves_dump(self) -> None:
        output, requests = self._run(
            [
                _START,
                _Reply(
                    HTTPMethod.GET,
                    body=b'{"active":true,"action":"restore_database","task":"error"}',
                ),
                _DELETE,
            ],
            success=False,
            deleted=False,
        )
        self.assertNotIn(HTTPMethod.DELETE, requests)
        self.assertIn("Database restore failed", output)

    def test_polling_recovers_from_connection_refusal(self) -> None:
        output, _ = self._run(
            [_START, dataclasses.replace(_ACTIVE, outage=True), _END, _DELETE],
            success=True,
            deleted=True,
        )
        self.assertIn("curl: (7)", output)

    def test_cleanup_recovers_from_connection_refusal(self) -> None:
        output, _ = self._run(
            [_START, _ACTIVE, dataclasses.replace(_END, outage=True), _DELETE],
            success=True,
            deleted=True,
        )
        self.assertIn("curl: (7)", output)

    def test_idle_before_restore_is_not_completion(self) -> None:
        _, requests = self._run(
            [_START, _END, _ACTIVE, _END, _DELETE], success=True, deleted=True
        )
        self.assertEqual(requests.count(HTTPMethod.GET), 3)

    def test_unconfirmed_completion_times_out_without_cleanup(self) -> None:
        for status in (_ACTIVE, _END):
            with self.subTest(status=status):
                output, requests = self._run(
                    [_START, status], success=False, deleted=False
                )
                self.assertNotIn(HTTPMethod.DELETE, requests)
                self.assertIn("Timed out", output)

    def test_invalid_status_preserves_dump(self) -> None:
        for body in (b"invalid-json", b"{}", b'{"active":"false","action":"end"}'):
            with self.subTest(body=body):
                output, requests = self._run(
                    [_START, _Reply(HTTPMethod.GET, body=body)],
                    success=False,
                    deleted=False,
                )
                self.assertNotIn(HTTPMethod.DELETE, requests)
                self.assertIn("Invalid restore status", output)

    def test_polling_authentication_failure_is_not_retried(self) -> None:
        for status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            with self.subTest(status=status):
                _, requests = self._run(
                    [_START, _Reply(HTTPMethod.GET, status=status)],
                    success=False,
                    deleted=False,
                )
                self.assertEqual(requests, [HTTPMethod.POST, HTTPMethod.GET])

    def test_restore_request_is_not_retried(self) -> None:
        _, requests = self._run(
            [_Reply(HTTPMethod.POST, status=HTTPStatus.SERVICE_UNAVAILABLE)],
            success=False,
            deleted=False,
        )
        self.assertEqual(requests, [HTTPMethod.POST])

    def test_polling_recovers_from_http_unavailability(self) -> None:
        self._run(
            [
                _START,
                _Reply(HTTPMethod.GET, status=HTTPStatus.SERVICE_UNAVAILABLE),
                _ACTIVE,
                _END,
                _DELETE,
            ],
            success=True,
            deleted=True,
        )

    def test_polling_outage_times_out_without_cleanup(self) -> None:
        output, requests = self._run(
            [_START, _Reply(HTTPMethod.GET, status=HTTPStatus.SERVICE_UNAVAILABLE)],
            success=False,
            deleted=False,
        )
        self.assertNotIn(HTTPMethod.DELETE, requests)
        self.assertIn("Could not confirm database restore completion", output)

    def test_cleanup_authentication_failure_is_distinct(self) -> None:
        output, requests = self._run(
            [
                _START,
                _ACTIVE,
                _END,
                _Reply(HTTPMethod.DELETE, status=HTTPStatus.FORBIDDEN),
            ],
            success=False,
            deleted=False,
        )
        self.assertEqual(requests.count(HTTPMethod.DELETE), 1)
        self.assertIn(
            "Database restore succeeded, but staged dump cleanup failed", output
        )

    def test_cleanup_timeout_is_distinct(self) -> None:
        output, _ = self._run(
            [
                _START,
                _ACTIVE,
                _END,
                _Reply(HTTPMethod.DELETE, status=HTTPStatus.SERVICE_UNAVAILABLE),
            ],
            success=False,
            deleted=False,
        )
        self.assertIn(
            "Database restore succeeded, but staged dump cleanup failed", output
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
