"""Build-time extension for the pinned Wyoming image; no ASR code is vendored."""
import hashlib
from pathlib import Path
import sys

EXPECTED = 'df2fbd3b2364427db8462b20bc5a3e6f41bf6d22f4d3a61b95c32aa88894310d'


def apply(path):
    original = path.read_bytes()
    if hashlib.sha256(original).hexdigest() != EXPECTED:
        raise RuntimeError('Upstream speech code changed; review the decoding extension before rebuilding.')
    text = original.decode('utf-8')
    changes = [
        ('import logging\n', 'import logging\nimport os\nimport wave\n'),
        ('        self.vad_filter = vad_parameters is not None\n',
         '        policy = os.getenv("ATHENA_WHISPER_SINGLE_PASS", "false").strip().casefold()\n'
         '        if policy not in {"true", "false"}:\n'
         '            raise ValueError("ATHENA_WHISPER_SINGLE_PASS must be true or false.")\n'
         '        self.single_pass = policy == "true"\n'
         '        self.vad_filter = vad_parameters is not None\n'),
        ('        if self.task:\n',
         '        if self.single_pass:\n'
         '            kwargs["temperature"] = [0.0]\n'
         '            kwargs["log_prob_threshold"] = None\n'
         '        if self.task:\n'),
        ('        return text\n',
         '        if self.single_pass:\n'
         '            # Check after inference so synthetic startup audio can prime the model.\n'
         '            with wave.open(str(wav_path), "rb") as audio:\n'
         '                if audio.getsampwidth() == 2 and not any(audio.readframes(audio.getnframes())):\n'
         '                    return ""\n'
         '        return text\n'),
    ]
    for before, after in changes:
        if text.count(before) != 1:
            raise RuntimeError('Unexpected upstream speech implementation.')
        text = text.replace(before, after)
    compile(text, str(path), 'exec')
    path.write_text(text, encoding='utf-8')
    print('Installed the opt-in single-pass decoding policy.')


if __name__ == '__main__':
    apply(Path(sys.argv[1]))
