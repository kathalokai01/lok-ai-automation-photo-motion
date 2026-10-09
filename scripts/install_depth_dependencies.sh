
#!/usr/bin/env bash
set -euo pipefail

echo "Installing CPU-only PyTorch for depth estimation..."
python -m pip install --no-cache-dir \
  torch \
  --index-url https://download.pytorch.org/whl/cpu

echo "Installing depth estimation dependencies..."
python -m pip install --no-cache-dir transformers

python - <<'PY'
import torch
import transformers
from PIL import Image

print("PyTorch:", torch.__version__)
print("Transformers:", transformers.__version__)
print("Pillow:", Image.__module__)
print("Depth estimation dependencies installed.")
PY
