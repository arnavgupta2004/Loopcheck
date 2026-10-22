"""Image loading and validation."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}
MAX_FILE_BYTES = 30 * 1024 * 1024
MIN_SIDE = 64
MAX_SIDE = 12000


class InputError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def load_image(path: str | Path) -> tuple[np.ndarray, dict]:
    """Read an image file and return (8-bit BGR array, info)."""
    path = Path(path)
    if not path.is_file():
        raise InputError("file_not_found", f"{path} does not exist or is not a file")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise InputError("unsupported_type", f"{path.suffix!r} not in {sorted(SUPPORTED_EXTENSIONS)}")
    size = path.stat().st_size
    if size == 0:
        raise InputError("empty_file", f"{path} is empty")
    if size > MAX_FILE_BYTES:
        raise InputError("file_too_large", f"{path} is {size} bytes (limit {MAX_FILE_BYTES})")
    raw = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise InputError("decode_failed", f"OpenCV could not decode {path}")
    img, info = validate_array(raw)
    info.update({"path": str(path), "file_bytes": size})
    return img, info


def validate_array(raw: np.ndarray) -> tuple[np.ndarray, dict]:
    """Normalise an in-memory image to 8-bit, 3-channel BGR and validate its size."""
    if not isinstance(raw, np.ndarray) or raw.ndim not in (2, 3):
        raise InputError("bad_shape", "image must be a 2-D or 3-D array")
    channels = 1 if raw.ndim == 2 else raw.shape[2]
    info = {"original_shape": list(raw.shape), "original_dtype": str(raw.dtype), "original_channels": channels}

    if raw.dtype == np.uint16:
        raw = (raw / 257.0).round().astype(np.uint8)
    elif raw.dtype != np.uint8:
        raise InputError("unsupported_dtype", f"dtype {raw.dtype} is not supported (need uint8 or uint16)")

    if channels == 1:
        img = cv2.cvtColor(raw if raw.ndim == 2 else raw[:, :, 0], cv2.COLOR_GRAY2BGR)
    elif channels == 3:
        img = raw
    elif channels == 4:
        img = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)
    else:
        raise InputError("unsupported_channels", f"{channels} channels (need 1, 3 or 4)")

    h, w = img.shape[:2]
    if min(h, w) < MIN_SIDE:
        raise InputError("image_too_small", f"{w}x{h} is below the {MIN_SIDE}px minimum side")
    if max(h, w) > MAX_SIDE:
        raise InputError("image_too_large", f"{w}x{h} exceeds the {MAX_SIDE}px maximum side")
    if float(img.std()) < 1.0:
        raise InputError("blank_image", "image is (nearly) uniform; nothing to inspect")
    info["shape"] = [h, w, 3]
    return np.ascontiguousarray(img), info
