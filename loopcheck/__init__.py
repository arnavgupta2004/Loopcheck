"""LoopCheck: deterministic reference-to-inspection comparison on OpenCV 5."""

import cv2

if not cv2.__version__.startswith("5."):
    raise ImportError(f"LoopCheck requires OpenCV 5.x, found cv2 {cv2.__version__}")

__version__ = "0.1.0"
OPENCV_VERSION = cv2.__version__
