"""The machine disko runs on, as the app talks about it: an Apple Silicon Mac (the deep pass and Kev on
MLX) or Linux (both on PyTorch, on an NVIDIA GPU when there is one)."""

import sys

MAC = sys.platform == "darwin"
THIS_MACHINE = "this Mac" if MAC else "this computer"
