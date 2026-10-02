import argparse
import signal
import sys
import time

from requests.exceptions import RequestException

from router import route, voice_targets


def main():
    parser = argparse.ArgumentParser(description="Athena voice commands")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--text", action="store_true", help="type commands instead of recording")
    modes.add_argument("--manual", action="store_true", help="use Space to start and stop recording")
    args = parser.parse_args()
    fatal_voice_errors = ()
    if not args.text:
        if args.manual and not sys.stdin.isatty():
            print("Manual recording requires an interactive terminal. Use wake-word mode or --text.")
            return 1
        try:
            if args.manual:
                from voice.input import record
            else:
                from voice.wake import record, strip_wake_phrase, WakeSetupError
                fatal_voice_errors = (WakeSetupError,)
            from voice.stt import transcribe
            from resolver import normalize, resolve
        except (ImportError, OSError) as error:
            print(f"Voice setup unavailable: {error}. See voice/README.md or use --text.")
            return 1

    print("Athena — Ctrl-C to quit." if not args.text else "Athena — type a command, or exit to quit.")
    print("Try: turn on nanoleafs")

    def stop(signum, frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            if args.text:
                command = input("athena> ").strip()
            else:
                try:
                    audio = record()
                    if not audio:
                        print("No command heard. Listening again.")
                        continue
                    print("Transcribing...", flush=True)
                    transcript = transcribe(audio)
                    print(f"Heard: {transcript}" if transcript else "No speech recognized. Try again.")
                    spoken_command = transcript if args.manual else strip_wake_phrase(transcript)
                    command = resolve(spoken_command, voice_targets())
                    if command != normalize(spoken_command):
                        print(f"Matched: {command}")
                except fatal_voice_errors as error:
                    print(f"Wake-word setup unavailable: {error}. See voice/README.md.")
                    return 1
                except RuntimeError as error:
                    print(error)
                    if not args.manual:
                        time.sleep(1)
                    continue
                except ValueError as error:
                    print(error)
                    continue
            if command.lower() in {"exit", "quit"}:
                break
            if not command:
                continue

            try:
                action = route(command)
            except RequestException:
                print("Service request failed. Check the connection and configuration, then try again.")
            except ValueError as error:
                print(error)
            else:
                if action.backend == "desktop" and action.action == "wake":
                    print("Wake packet sent. PC startup is not confirmed.")
                elif action.backend == "desktop" and action.action in {"shutdown", "restart", "sleep"}:
                    print(f"{action.action.capitalize()} accepted by the desktop client.")
                else:
                    print(f"Done: {action.action} {action.target}")
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)

    print("Goodbye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
