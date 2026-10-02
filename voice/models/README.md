# Custom wake model

Place an openWakeWord model trained for the single word **Athena** here as
`athena.tflite`. The model stays outside Git. A model trained for "Hey Athena"
will still recognize that phrase if you rename its file; it must be trained for
the desired spoken word.

Use [openWakeWord's training notebook](https://github.com/dscripka/openWakeWord#training-new-models)
or [Home Assistant's training guide](https://www.home-assistant.io/voice_control/create_wake_word/).
Set the target word to `athena`, preview its pronunciation, run training, and
download the `.tflite` output. This is a one-time training step; runtime detection
uses the local Wyoming service. Training requires the resources and sign-in
described by the notebook and is not performed by Athena.

This service expects an **openWakeWord** classifier, not a microWakeWord model
or a Porcupine `.ppn` file. Once the model is present, start the service with
`docker compose -f voice/compose.yaml up -d` from the repository root. Restart
the service after adding or replacing models.
