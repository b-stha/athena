"""Lazy, serialized async agent ownership for synchronous OVOS callbacks."""

import asyncio
from queue import Empty, Full, Queue
import threading

from .dispatcher import RequestDispatcher


class AgentWorker:
    def __init__(self, *, factory=None):
        self.factory = factory or RequestDispatcher
        self.queue = Queue(maxsize=2)
        self.lock = threading.Lock()
        self.thread = None
        self.loop = None
        self.active = None
        self.closed = False

    def submit(self, text, on_result, on_error, *, session_id="default"):
        with self.lock:
            if self.closed:
                return False
            try:
                self.queue.put_nowait((text, session_id, on_result, on_error))
            except Full:
                return False
            if self.thread is None:
                self.thread = threading.Thread(target=lambda: asyncio.run(self._run()), name="athena-agent", daemon=True)
                self.thread.start()
            return True

    async def _run(self):
        with self.lock:
            self.loop = asyncio.get_running_loop()
        agent = None
        try:
            while not self.closed:
                item = await asyncio.to_thread(self.queue.get)
                if item is None or self.closed:
                    return
                text, session_id, on_result, on_error = item
                try:
                    if agent is None:
                        agent = self.factory()
                    # close() can run while a slow factory builds the agent.
                    # Publish work atomically so it either never starts or is cancellable.
                    with self.lock:
                        if self.closed:
                            return
                        self.active = asyncio.create_task(agent.turn(text, session_id=session_id))
                    result = await self.active
                    if not self.closed:
                        on_result(result)
                except asyncio.CancelledError:
                    return
                except Exception:
                    if not self.closed:
                        on_error("Athena's agent failed. Check model/MCP configuration and the service log.")
                finally:
                    with self.lock:
                        self.active = None
        finally:
            if agent is not None:
                await agent.close()
            with self.lock:
                self.loop = None

    def close(self):
        with self.lock:
            self.closed = True
            while True:
                try:
                    self.queue.get_nowait()
                except Empty:
                    break
            self.queue.put_nowait(None)
            if self.loop is not None and self.active is not None:
                self.loop.call_soon_threadsafe(self.active.cancel)
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError("Athena agent did not stop within its shutdown deadline.")
