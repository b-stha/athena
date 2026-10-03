"""Configure and supervise the OVOS services for this checkout."""

import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from athena_ovos.setup import resolve_model_path


def build_config(model, *, device="reSpeaker Flex XVF3800", bus_port=8181,
                 wake_sensitivity=0.5, wake_trigger_level=3, stt_timeout=30):
    if not 1 <= bus_port <= 65535:
        raise ValueError("Bus port must be between 1 and 65535.")
    if (isinstance(wake_sensitivity, bool) or not isinstance(wake_sensitivity, (int, float))
            or not math.isfinite(wake_sensitivity) or not 0 <= wake_sensitivity <= 1):
        raise ValueError("Wake sensitivity must be a finite number between 0 and 1.")
    if (isinstance(wake_trigger_level, bool) or not isinstance(wake_trigger_level, int)
            or wake_trigger_level < 0):
        raise ValueError("Wake trigger level must be a nonnegative integer.")
    if (isinstance(stt_timeout, bool) or not isinstance(stt_timeout, (int, float))
            or not math.isfinite(stt_timeout) or stt_timeout <= 0):
        raise ValueError("Transcription timeout must be a finite positive number of seconds.")
    return {
        "lang": "en-us", "secondary_langs": [], "confirm_listening": False,
        "sounds": {key: "" for key in ("start_listening", "end_listening", "acknowledge", "error")},
        "websocket": {"host": "127.0.0.1", "port": bus_port, "route": "/core", "ssl": False},
        "gui": {"disable_gui": True},
        "utterance_transformers": {}, "intent_transformers": {},
        "intents": {"pipeline": ["adapt_high", "adapt_medium", "adapt_low", "fallback_low"]},
        "skills": {"fallbacks": {"fallback_mode": "whitelist", "fallback_whitelist": ["athena-skill"]}},
        "listener": {
            "sample_rate": 16000, "wake_word": "athena", "stand_up_word": "athena",
            "instant_listen": True, "continuous_listen": False, "hybrid_listen": False,
            "retry_mic_init": False, "speech_begin": 0.1, "silence_end": 0.8,
            "recording_timeout": 30, "recording_timeout_with_silence": 5,
            "utterance_chunks_to_rewind": 10, "remove_silence": True,
            "record_wake_words": False, "save_utterances": False,
            "microphone": {
                "module": "athena-microphone",
                "athena-microphone": {
                    "device": device, "capture_channels": 2, "source_channel": 2,
                    "sample_rate": 16000, "sample_width": 2, "sample_channels": 1,
                    "chunk_size": 640,
                },
            },
            "VAD": {
                "module": "athena-webrtcvad", "silence_method": "vad_only",
                "athena-webrtcvad": {"vad_mode": 2},
            },
        },
        "hotwords": {
            "athena": {"module": "athena-precise-onnx", "model": str(model),
                       "active": True, "listen": True, "wakeup": True,
                       "sensitivity": wake_sensitivity, "trigger_level": wake_trigger_level},
            **{word: {"active": False} for word in (
                "hey_mycroft", "hey_mycroft_precise", "hey_mycroft_vosk",
                "hey_mycroft_pocketsphinx", "wake_up", "wake_up_pocketsphinx")},
        },
        "stt": {"module": "athena-wyoming-whisper", "fallback_module": "",
                "athena-wyoming-whisper": {"timeout": stt_timeout, "connect_timeout": 5}},
    }


def prepare_environment(root, model=None, *, device="reSpeaker Flex XVF3800", bus_port=8181,
                        wake_sensitivity=0.5, wake_trigger_level=3, stt_timeout=30):
    root = Path(root).resolve()
    model = Path(model).expanduser().resolve() if model else resolve_model_path(root)
    if not model.is_file() or not model.stat().st_size:
        raise ValueError("Athena wake model is missing. Run .venv/bin/python -m athena_ovos.setup.")
    config = build_config(model, device=device, bus_port=bus_port,
                          wake_sensitivity=wake_sensitivity, wake_trigger_level=wake_trigger_level,
                          stt_timeout=stt_timeout)
    state = root / ".ovos"
    config_dir = state / "config" / "athena"
    config_dir.mkdir(parents=True, exist_ok=True)
    # Generated from launcher options; keep credentials in .env.
    (config_dir / "mycroft.conf").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "OVOS_CONFIG_BASE_FOLDER": "athena", "OVOS_CONFIG_FILENAME": "mycroft.conf",
        "XDG_CONFIG_HOME": str(state / "config"), "XDG_DATA_HOME": str(state / "data"),
        "XDG_CACHE_HOME": str(state / "cache"), "PYTHONUNBUFFERED": "1",
    })
    return env, config


def check_plugins():
    required = {"opm.skill": "athena-skill", "opm.microphone": "athena-microphone",
                "opm.stt": "athena-wyoming-whisper", "opm.VAD": "athena-webrtcvad",
                "opm.wake_word": "athena-precise-onnx"}
    for group, name in required.items():
        matches = [e for e in importlib.metadata.entry_points(group=group) if e.name == name]
        if not matches:
            raise ValueError("Athena's OVOS plugins are not installed. Run .venv/bin/python -m pip install -e .")
        matches[0].load()


def stop_processes(processes, *, timeout=8):
    """Stop listener/skills before the bus; enforce a shared shutdown deadline."""
    deadline = time.monotonic() + timeout
    for process in reversed(processes):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def _check_processes(processes):
    for name, process in processes:
        if process.poll() is not None:
            raise RuntimeError(f"OVOS {name} exited unexpectedly ({process.returncode}). Check .ovos logs.")


def run(root, env, config, *, startup_timeout=60):
    if sys.platform == "win32":
        raise ValueError("Run Athena's OVOS frontend on the Pi/Linux; use --text to test backends on Windows.")
    port = config["websocket"]["port"]
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise ValueError(f"OVOS bus port {port} is already in use. Stop the other instance or use --bus-port.") from error
    processes = []
    bus = None
    os.environ.update(env)
    from ovos_bus_client import MessageBusClient

    def start(name, *arguments):
        process = subprocess.Popen([sys.executable, "-m", *arguments], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL, start_new_session=True)
        processes.append((name, process))

    def wait_until(predicate, name):
        deadline = time.monotonic() + startup_timeout
        while not predicate():
            _check_processes(processes)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for OVOS {name}. Check .ovos logs.")
            time.sleep(0.1)

    try:
        start("messagebus", "ovos_messagebus")
        def port_ready():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                return False
        wait_until(port_ready, "messagebus")
        bus = MessageBusClient(host="127.0.0.1", port=port, route="/core", ssl=False)
        skill_ready = threading.Event()
        bus.on("mycroft.skill.loaded", lambda message: skill_ready.set()
               if message.data.get("skill_id") == "athena-skill" else None)
        bus.on("speak", lambda message: print(message.data.get("utterance", ""), flush=True))
        bus.on("recognizer_loop:speech.recognition.unknown", lambda message:
               print("Could not transcribe the command. Say 'Athena' to try again.", flush=True))
        bus.on("recognizer_loop:utterance", lambda message:
               print("Heard: " + str(message.data.get("utterances", [""])[0]), flush=True)
               if message.data.get("utterances") else None)
        bus.run_in_thread()
        wait_until(bus.connected_event.is_set, "messagebus connection")
        start("skills", "ovos_core", "--disable-installer")
        wait_until(skill_ready.is_set, "Athena skill")
        with tempfile.TemporaryDirectory(prefix="athena-ready-") as ready_dir:
            ready_path = Path(ready_dir) / "listener"
            start("listener", "athena_ovos.service", str(ready_path))
            wait_until(ready_path.is_file, "listener")
            print("Athena is listening. Say 'Athena', then your command. Ctrl-C to stop.", flush=True)
            while True:
                _check_processes(processes)
                time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        if bus is not None:
            bus.close()
        stop_processes([process for _, process in processes])
