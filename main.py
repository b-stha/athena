from requests.exceptions import RequestException

from router import route


def main():
    print("Athena — type a command, or exit to quit.")
    print("Try: turn on desk lights")

    try:
        while True:
            command = input("athena> ").strip()
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
                print(f"Done: {action.action} {action.target}")
    except (EOFError, KeyboardInterrupt):
        print()

    print("Goodbye.")


if __name__ == "__main__":
    main()
