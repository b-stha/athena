import argparse
import os
from pathlib import Path
import signal
import sys

from dotenv import load_dotenv
from requests.exceptions import RequestException


def text_mode():
    from router import route
    print("Athena - type a command, or exit to quit.")
    while True:
        try:
            command = input("athena> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if command.lower() in {"exit", "quit"}:
            return 0
        if not command:
            continue
        try:
            action = route(command)
            if action.backend == "desktop" and action.action == "wake":
                print("Wake packet sent. PC startup is not confirmed.")
            elif action.backend == "desktop" and action.action in {"shutdown", "restart", "sleep"}:
                print(f"{action.action.capitalize()} accepted by the desktop client.")
            else:
                print(f"Done: {action.action} {action.target}")
        except (ValueError, RequestException) as error:
            print(f"Command failed: {error}")


def main():
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    parser = argparse.ArgumentParser(description="Athena's OVOS voice frontend")
    parser.add_argument("--text", action="store_true", help="type commands to test the backends")
    parser.add_argument("--model", type=Path, help="use a different Precise Athena ONNX model")
    parser.add_argument("--mic-device", default="reSpeaker Flex XVF3800", help="PortAudio microphone name")
    parser.add_argument("--bus-port", type=int, default=8181, help="local OVOS bus port")
    parser.add_argument("--check-config", action="store_true", help="validate plugins and model without recording")
    args = parser.parse_args()

    def stop(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        if args.text:
            return text_mode()
        from athena_ovos.runtime import check_plugins, prepare_environment, run
        env, config = prepare_environment(root, args.model, device=args.mic_device, bus_port=args.bus_port)
        os.environ.update(env)
        check_plugins()
        if args.check_config:
            from ovos_plugin_manager.microphone import OVOSMicrophoneFactory
            from ovos_plugin_manager.stt import OVOSSTTFactory
            from ovos_plugin_manager.vad import OVOSVADFactory
            from ovos_plugin_manager.wakewords import OVOSWakeWordFactory
            microphone = OVOSMicrophoneFactory.create()
            OVOSVADFactory.create().is_silence(bytes(microphone.chunk_size))
            OVOSSTTFactory.create()
            try:
                OVOSWakeWordFactory.create_hotword("athena")
            except Exception as error:
                raise ValueError(f"Cannot load Athena's wake model: {error}") from error
            print("Athena's OVOS plugins and wake model are ready. Microphone was not opened.")
            return 0
        return run(root, env, config)
    except KeyboardInterrupt:
        return 0
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        print(f"Athena setup failed: {error}. See voice/README.md.", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
