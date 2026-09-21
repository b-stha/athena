import argparse
import sys

from requests.exceptions import RequestException

from router import route


def main():
    parser = argparse.ArgumentParser(description="Athena voice command terminal")
    parser.add_argument("--text", action="store_true", help="type commands instead of recording")
    args = parser.parse_args()
    if not args.text:
        if not sys.stdin.isatty():
            print("Voice input requires an interactive terminal. Use --text for piped input.")
            return
        try:
            from voice.input import record
            from voice.stt import transcribe
        except (ImportError, OSError) as error:
            print(f"Voice setup unavailable: {error}. See voice/README.md or use --text.")
            return

    print("Athena — Ctrl-C to quit." if not args.text else "Athena — type a command, or exit to quit.")
    print("Try: turn on nanoleafs")

    try:
        while True:
            if args.text:
                command = input("athena> ").strip()
            else:
                try:
                    audio = record()
                    print("Transcribing...", flush=True)
                    transcript = transcribe(audio)
                except (RuntimeError, ValueError) as error:
                    print(error)
                    continue
                print(f"Heard: {transcript}" if transcript else "No speech recognized. Try again.")
                # Whisper may add sentence punctuation to a fixed command.
                command = transcript.strip().rstrip(".!?").strip()
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
                else:
                    print(f"Done: {action.action} {action.target}")
    except (EOFError, KeyboardInterrupt):
        print()

    print("Goodbye.")


if __name__ == "__main__":
    main()
