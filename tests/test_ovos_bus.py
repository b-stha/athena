"""Run the real OVOS bus/core with a fake agent and no microphone or cloud calls."""

import json
import os
from pathlib import Path
from queue import Empty, Queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ovos_bus_client import MessageBusClient
from ovos_bus_client.message import Message

from athena_ovos.runtime import prepare_environment, stop_processes


@unittest.skipUnless(os.name == "posix", "OVOS services run on Linux")
class OVOSBusIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="athena-bus-test-")
        cls.root = Path(cls.temporary.name)
        cls.processes = []
        cls.bus = None
        cls.http = None
        cls.log = None
        cls.requests = []
        cls.request_lock = threading.Lock()
        cls.results = Queue()
        cls.context_requests = Queue()
        cls.skill_loaded = threading.Event()
        cls.skills_initialized = threading.Event()
        cls.startup_requests = []
        cls.startup_errors = []
        cls.startup_guard_hit = cls.root / "forbidden-startup-operation"
        cls.startup_guard_ready = cls.root / "startup-guard-installed"
        try:
            class LoopbackBackend(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(length))
                    with cls.request_lock:
                        cls.requests.append((self.path, body, self.headers.get("Authorization")))
                    if self.path == "/commands":
                        response = {"requestId": body["requestId"], "success": True,
                                    "message": "Fake desktop accepted."}
                    else:
                        self.send_error(404)
                        return
                    payload = json.dumps(response).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            cls.http = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackBackend)
            cls.http.daemon_threads = True
            cls.http_thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
            cls.http_thread.start()
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                bus_port = reserve.getsockname()[1]
            model = cls.root / "unused-model.onnx"
            model.write_bytes(b"Listener intentionally absent in this bus/core test.")
            env, _ = prepare_environment(cls.root, model, bus_port=bus_port)
            http_url = f"http://127.0.0.1:{cls.http.server_port}"
            env.update({"HOME": str(cls.root),
                        "XDG_CONFIG_DIRS": str(cls.root / "empty-system-config"),
                        "DESKTOP_URL": http_url})
            cls.log = (cls.root / "services.log").open("w+", encoding="utf-8")

            # Apply the sentinel only to this test's skills child. It prevents
            # external probes while allowing the existing loopback backends.
            guard_dir = cls.root / "startup-guard"
            guard_dir.mkdir()
            guard_source = "from pathlib import Path\nSENTINEL = Path(" + repr(str(cls.startup_guard_hit)) + ")\n"
            guard_source += "READY = Path(" + repr(str(cls.startup_guard_ready)) + ")\n"
            guard_source += """
from urllib.parse import urlsplit
import requests.sessions
import ovos_core.skill_manager as manager
from ovos_bus_client.client import MessageBusClient

def forbidden(reason):
    SENTINEL.write_text(reason, encoding="utf-8")
    raise RuntimeError("Forbidden startup operation: " + reason)

manager.is_connected_http = lambda *args, **kwargs: forbidden("direct HTTP connectivity probe")
original_request = requests.sessions.Session.request

def local_request(self, method, url, *args, **kwargs):
    if urlsplit(str(url)).hostname not in ("127.0.0.1", "localhost"):
        forbidden("external HTTP request")
    return original_request(self, method, url, *args, **kwargs)

requests.sessions.Session.request = local_request
original_wait = MessageBusClient.wait_for_response

def local_wait(self, message, *args, **kwargs):
    if message.msg_type in ("mycroft.skills.train", "ovos.skills.train", "ovos.PHAL.internet_check"):
        forbidden(message.msg_type)
    return original_wait(self, message, *args, **kwargs)

MessageBusClient.wait_for_response = local_wait
# Only the reasoning dependency is fake; the bus, core and skill are real.
import athena_agent.worker as worker
class FakeAgent:
    async def turn(self, text, session_id="default"):
        return "Handled: " + text
    async def close(self):
        pass
original_init = worker.AgentWorker.__init__
def fake_init(self, **kwargs):
    original_init(self, factory=FakeAgent)
worker.AgentWorker.__init__ = fake_init
READY.write_text("ready", encoding="utf-8")
"""
            (guard_dir / "sitecustomize.py").write_text(guard_source, encoding="utf-8")

            def start(module, *arguments):
                process_env = env
                if module == "athena_ovos.skills":
                    process_env = env.copy()
                    process_env["PYTHONPATH"] = str(guard_dir) + os.pathsep + env.get("PYTHONPATH", "")
                process = subprocess.Popen([sys.executable, "-m", module, *arguments],
                                           cwd=cls.root, env=process_env,
                                           stdin=subprocess.DEVNULL, stdout=cls.log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                cls.processes.append(process)

            start("ovos_messagebus")

            def port_ready():
                try:
                    with socket.create_connection(("127.0.0.1", bus_port), timeout=0.1):
                        return True
                except OSError:
                    return False

            cls.wait_until(port_ready, "messagebus socket")
            cls.bus = MessageBusClient(host="127.0.0.1", port=bus_port, route="/core", ssl=False)
            cls.bus.on("mycroft.skill.loaded",
                       lambda message: cls.skill_loaded.set()
                       if message.data.get("skill_id") == "athena-skill" else None)
            cls.bus.on("athena.action.result", cls.results.put)
            cls.bus.on("athena.context.request", cls.context_requests.put)
            cls.bus.on("mycroft.skills.initialized", lambda message: cls.skills_initialized.set())
            cls.bus.on("mycroft.skills.train", lambda message: cls.startup_requests.append(message.msg_type))
            cls.bus.on("ovos.PHAL.internet_check", lambda message: cls.startup_requests.append(message.msg_type))
            cls.bus.on("mycroft.skills.error", cls.startup_errors.append)
            # Observe before launching core, so fast skill startup cannot race
            # the readiness listener.
            cls.bus.run_in_thread()
            cls.wait_until(cls.bus.connected_event.is_set, "observer connection")
            started = time.monotonic()
            start("athena_ovos.skills")
            cls.wait_until(cls.skill_loaded.is_set, "Athena skill readiness", timeout=10)
            cls.wait_until(cls.skills_initialized.is_set, "skill manager initialization", timeout=10)
            cls.startup_seconds = time.monotonic() - started
        except BaseException:
            cls.cleanup()
            raise

    @classmethod
    def log_tail(cls):
        if cls.log is None:
            return ""
        cls.log.flush()
        return (cls.root / "services.log").read_text(errors="replace")[-12000:]

    @classmethod
    def wait_until(cls, predicate, description, timeout=30):
        deadline = time.monotonic() + timeout
        while not predicate():
            exited = [process.returncode for process in cls.processes if process.poll() is not None]
            if exited:
                raise AssertionError(f"OVOS process exited {exited} waiting for {description}.\n{cls.log_tail()}")
            if time.monotonic() >= deadline:
                raise AssertionError(f"Timed out waiting for {description}.\n{cls.log_tail()}")
            time.sleep(0.02)

    @classmethod
    def cleanup(cls):
        try:
            stop_processes(cls.processes, timeout=3)
        finally:
            if cls.bus is not None:
                cls.bus.close()
            if cls.http is not None:
                cls.http.shutdown()
                cls.http.server_close()
            if cls.log is not None:
                cls.log.close()
            cls.temporary.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls.cleanup()

    def setUp(self):
        for queue in [self.results, self.context_requests]:
            while True:
                try:
                    queue.get_nowait()
                except Empty:
                    break
        with self.request_lock:
            self.requests.clear()

    def emit_utterance(self, utterance):
        self.bus.emit(Message("recognizer_loop:utterance",
                              {"utterances": [utterance], "lang": "en-us"},
                              {"source": "athena-bus-test",
                               "session": {"session_id": "athena-bus-test", "lang": "en-us"}}))

    def receive(self, queue):
        try:
            return queue.get(timeout=8)
        except Empty:
            self.fail(f"No expected Athena bus event.\n{self.log_tail()}")

    def test_startup_finishes_without_training_or_connectivity_probes(self):
        self.assertLess(self.startup_seconds, 10, self.log_tail())
        self.assertEqual(self.startup_requests, [])
        self.assertEqual(self.startup_errors, [])
        self.assertTrue(self.startup_guard_ready.exists(), self.log_tail())
        self.assertFalse(self.startup_guard_hit.exists(), self.log_tail())

    def test_all_transcripts_reach_the_agent_once_without_rest_execution(self):
        for utterance in ["sleep my pc", "open notepad", "turn on bedroom", "do not sleep my pc", "put that to sleep", "turn on bedroom and bathroom"]:
            with self.subTest(utterance=utterance):
                self.emit_utterance(utterance)
                context = self.receive(self.context_requests)
                self.assertEqual(context.data, {"utterance": utterance, "reason": "agent_request"})
                result = self.receive(self.results)
                self.assertEqual(result.data, {"status": "agent_response", "response": "Handled: " + utterance})
                time.sleep(0.1)
                with self.request_lock:
                    self.assertEqual(self.requests, [])
                self.assertTrue(self.results.empty())
                self.assertTrue(self.context_requests.empty())


if __name__ == "__main__":
    unittest.main()
