FROM rhasspy/wyoming-whisper@sha256:ea74afa3e3c347cd031d946b4950b7438ae4bfd07ef96a04ac8a7095f36f4ac6

COPY apply_decode_policy.py /usr/local/lib/athena/apply_decode_policy.py
RUN /usr/src/.venv/bin/python3 /usr/local/lib/athena/apply_decode_policy.py \
    /usr/src/wyoming_faster_whisper/faster_whisper_handler.py
