"""The machine disko runs on, as the app talks about it: an Apple Silicon Mac (the deep pass and Kev on
MLX) or Linux (both on PyTorch, on an NVIDIA GPU when there is one)."""

import sys

MAC = sys.platform == "darwin"
THIS_MACHINE = "this Mac" if MAC else "this computer"
MACHINE_RUNNING = "the Mac running" if MAC else "the computer running"  # "the sound output of ... disko"
LOCAL_LABEL = "This Mac" if MAC else "This computer"  # the local source, as listed in the status
SECRET_STORE = "Keychain" if MAC else "keyring"  # where sign-ins and keys are kept: "saved in the ..."
