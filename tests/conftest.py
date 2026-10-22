import numpy as np
import pytest

from loopcheck import synth


@pytest.fixture(scope="session")
def textured():
    """Random-shape grayscale-ish BGR texture with known content (480x640)."""
    rng = np.random.default_rng(7)
    import cv2

    img = np.full((480, 640, 3), 40, np.uint8)
    for _ in range(80):
        color = tuple(int(v) for v in rng.integers(60, 255, 3))
        x, y = int(rng.integers(0, 640)), int(rng.integers(0, 480))
        if rng.random() < 0.5:
            cv2.circle(img, (x, y), int(rng.integers(5, 35)), color, -1)
        else:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(10, 70)), y + int(rng.integers(10, 70))), color, -1)
    return img


@pytest.fixture(scope="session")
def case_factory():
    cache = {}

    def make(case_type: str, seed: int = 0):
        key = (case_type, seed)
        if key not in cache:
            cache[key] = synth.make_case(case_type, 1000 + seed)
        return cache[key]

    return make
