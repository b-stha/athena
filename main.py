import argparse
import os
from pathlib import Path
import signal
import sys

from dotenv import load_dotenv
from requests.exceptions import RequestException


def legacy_text_mode():
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


def text_mode():
    import asyncio
    from athena_agent.dispatcher import RequestDispatcher

    async def chat():
        agent = RequestDispatcher()
        print("Athena agent - type a command, or exit to quit.")
        try:
            while True:
                try:
                    command = input("athena> ").strip()
                except (EOFError, KeyboardInterrupt):
                    return 0
                if command.lower() in {"exit", "quit"}:
                    return 0
                if command:
                    print(await agent.turn(command))
        finally:
            await agent.close()
    return asyncio.run(chat())


def main():
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    os.environ.setdefault("ATHENA_AGENT_CONFIG", str(root / "agent.local.json"))
    parser = argparse.ArgumentParser(description="Athena's OVOS voice frontend")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--text", action="store_true", help="type requests for the same agent used by voice")
    modes.add_argument("--legacy-text", action="store_true", help="diagnose the retired deterministic REST backends")
    modes.add_argument("--check-agent-config", action="store_true", help="validate model/MCP settings without connecting")
    modes.add_argument("--check-local-config", action="store_true", help="validate direct HA settings without connecting")
    parser.add_argument("--wake-engine", choices=("custom", "precise"),
                        default=os.getenv("ATHENA_WAKE_ENGINE", "custom"),
                        help="wake detector (default: custom trained openWakeWord model)")
    parser.add_argument("--model", type=Path, help="ONNX classifier for the selected wake detector")
    parser.add_argument("--melspec-model", type=Path, help="custom openWakeWord mel-spectrogram ONNX model")
    parser.add_argument("--embedding-model", type=Path, help="custom openWakeWord embedding ONNX model")
    parser.add_argument("--wake-threshold", type=float, default=os.getenv("ATHENA_WAKE_THRESHOLD", "0.98"),
                        help="custom model activation threshold (default: 0.98)")
    parser.add_argument("--wake-consecutive", type=int, default=os.getenv("ATHENA_WAKE_CONSECUTIVE", "1"),
                        help="required consecutive fresh custom model scores (default: 1)")
    parser.add_argument("--wake-cooldown", type=float, default=os.getenv("ATHENA_WAKE_COOLDOWN", "2"),
                        help="custom wake cooldown in seconds of captured audio (default: 2)")
    parser.add_argument("--mic-device", default="reSpeaker Flex XVF3800", help="PortAudio microphone name")
    parser.add_argument("--bus-port", type=int, default=8181, help="local OVOS bus port")
    parser.add_argument("--wake-sensitivity", type=float, default=0.5,
                        help="legacy Precise sensitivity from 0 to 1; lower is stricter (default: 0.5)")
    parser.add_argument("--wake-trigger-level", type=int, default=3,
                        help="legacy Precise activation count; higher requires more evidence (default: 3)")
    parser.add_argument("--wake-diagnostics", action="store_true",
                        help="log wake model scores and microphone levels for troubleshooting")
    parser.add_argument("--stt-timeout", type=float, default=30,
                        help="seconds to wait for transcription after recording (default: 30)")
    parser.add_argument("--silence-end", type=float, default=os.getenv("ATHENA_SILENCE_END", "2"),
                        help="silence before ending an utterance (default: 2 seconds)")
    parser.add_argument("--command-wait", type=float, default=os.getenv("ATHENA_COMMAND_WAIT", "5"),
                        help="initial wait for speech after waking (default: 5 seconds)")
    parser.add_argument("--check-config", action="store_true", help="validate plugins and model without recording")
    args = parser.parse_args()

    def stop(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop)
    try:
        if args.legacy_text:
            return legacy_text_mode()
        if args.check_local_config:
            from athena_agent.local_ha import LocalHAConfig
            LocalHAConfig.from_env()
            print("Direct Home Assistant configuration is valid. No network request or device action was made.")
            return 0
        if args.check_agent_config:
            from athena_agent.config import AgentConfig
            config = AgentConfig.from_env()
            print("Agent configuration is valid. Configured MCP servers: " +
                  ", ".join(server.name for server in config.servers))
            print("No model request, MCP connection or device action was made.")
            return 0
        if args.text:
            return text_mode()
        from athena_ovos.runtime import check_plugins, prepare_environment, run
        env, config = prepare_environment(
            root, args.model, device=args.mic_device, bus_port=args.bus_port,
            wake_sensitivity=args.wake_sensitivity, wake_trigger_level=args.wake_trigger_level,
            stt_timeout=args.stt_timeout, wake_diagnostics=args.wake_diagnostics,
            wake_engine=args.wake_engine, melspec_model=args.melspec_model,
            embedding_model=args.embedding_model, wake_threshold=args.wake_threshold,
            wake_consecutive=args.wake_consecutive, wake_cooldown=args.wake_cooldown,
            silence_end=args.silence_end, command_wait=args.command_wait,
        )
        os.environ.update(env)
        check_plugins(config["hotwords"]["athena"]["module"])
        if args.wake_engine == "custom":
            print(f"Custom wake settings: threshold {args.wake_threshold:g}, "
                  f"consecutive {args.wake_consecutive}, cooldown {args.wake_cooldown:g}s.", flush=True)
        else:
            print(f"Precise wake settings: sensitivity {args.wake_sensitivity:g}, "
                  f"trigger level {args.wake_trigger_level}.", flush=True)
        print(f"Transcription timeout: {args.stt_timeout:g} seconds.", flush=True)
        print(f"Voice timing: initial wait {args.command_wait:g}s, end silence {args.silence_end:g}s.", flush=True)
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
