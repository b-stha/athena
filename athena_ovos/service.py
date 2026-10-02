"""Listener entry point that reports readiness and cleans up on SIGTERM."""

from pathlib import Path
import signal
import sys


def main():
    from ovos_dinkum_listener.service import OVOSDinkumVoiceService
    from ovos_plugin_manager.stt import OVOSSTTFactory
    from ovos_utils.log import init_service_logger
    from athena_ovos.streaming import BufferedWhisperSTT
    failed = []

    def ready():
        Path(sys.argv[1]).write_text("ready\n", encoding="utf-8")

    def error(reason="unknown"):
        failed.append(reason)
        print(f"OVOS listener failed: {reason}", file=sys.stderr, flush=True)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    init_service_logger("voice")
    service = OVOSDinkumVoiceService(on_ready=ready, on_error=error,
                                   stt=BufferedWhisperSTT(OVOSSTTFactory.create()))
    try:
        service.run()
    finally:
        # The native run() cleanup handles normal recording errors. It returns
        # before cleanup on a startup error, so cover that path here as well.
        if not service._shutdown_event.is_set():
            service.stop()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
