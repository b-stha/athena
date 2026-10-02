"""Install the public Athena wake-word model without requiring training."""

import argparse
import hashlib
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_MODEL = (
    "https://raw.githubusercontent.com/OpenVoiceOS/precise-lite-models/"
    "19274118d1525c12c4b4ebfe3e993f72828e3742/"
    "wakewords/en/athena-en-0.3.0-20190801-eltocino.onnx"
)
DEFAULT_MODEL_SHA256 = "41066784a4202927ca2387812f81d57438c78d9137ba6b798df393ebf78585d1"


def resolve_model_path(root: Path) -> Path:
    """Return the project's default model cache path."""
    return Path(root).expanduser().resolve() / ".ovos" / "models" / "athena.onnx"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as model:
        for chunk in iter(lambda: model.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_model(
    destination: Path,
    url: str = DEFAULT_MODEL,
    expected_sha256: str = DEFAULT_MODEL_SHA256,
) -> Path:
    """Cache a verified model, replacing an existing file only after verification."""
    destination = Path(destination).expanduser().resolve()
    expected_sha256 = expected_sha256.lower()
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise ValueError("Expected SHA256 must contain 64 hexadecimal characters.")
    if destination.is_file() and _sha256(destination) == expected_sha256:
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        digest = hashlib.sha256()
        with urlopen(url, timeout=30) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", suffix=".tmp",
                dir=destination.parent, delete=False,
            ) as model:
                temporary = Path(model.name)
                for chunk in iter(lambda: response.read(65536), b""):
                    model.write(chunk)
                    digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("Athena model checksum does not match the pinned release.")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install Athena's pretrained wake-word model")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parent.parent,
        help="Athena project directory (defaults to this checkout)",
    )
    parser.add_argument(
        "--model", type=Path,
        help="use an existing Precise ONNX model without downloading or copying it",
    )
    args = parser.parse_args(argv)
    try:
        if args.model is not None:
            model = args.model.expanduser().resolve()
            if not model.is_file() or model.stat().st_size == 0:
                raise ValueError(f"Model must be an existing nonempty file: {model}")
        else:
            model = download_model(resolve_model_path(args.root))
    except (OSError, URLError, ValueError) as error:
        print(f"Athena model setup failed: {error}", file=sys.stderr)
        return 1
    print(model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
