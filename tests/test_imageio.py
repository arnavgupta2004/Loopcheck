import cv2
import numpy as np
import pytest

from loopcheck.imageio import InputError, load_image, validate_array


def _code(fn, *a):
    with pytest.raises(InputError) as e:
        fn(*a)
    return e.value.code


def test_missing_file(tmp_path):
    assert _code(load_image, tmp_path / "nope.png") == "file_not_found"


def test_unsupported_extension(tmp_path):
    p = tmp_path / "x.gif"
    p.write_bytes(b"GIF89a")
    assert _code(load_image, p) == "unsupported_type"


def test_corrupt_file(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"not an image at all")
    assert _code(load_image, p) == "decode_failed"


def test_empty_file(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"")
    assert _code(load_image, p) == "empty_file"


def test_too_small_and_blank():
    assert _code(validate_array, np.random.default_rng(0).integers(0, 255, (32, 32, 3), dtype=np.uint8)) == "image_too_small"
    assert _code(validate_array, np.full((100, 100, 3), 128, np.uint8)) == "blank_image"


def test_unsupported_dtype_and_channels():
    assert _code(validate_array, np.random.default_rng(0).random((100, 100, 3)).astype(np.float32)) == "unsupported_dtype"
    assert _code(validate_array, np.zeros((100, 100, 2), np.uint8)) == "unsupported_channels"


def test_gray_bgra_and_16bit_are_normalised(tmp_path):
    rng = np.random.default_rng(0)
    gray = rng.integers(0, 255, (100, 120), dtype=np.uint8)
    img, info = validate_array(gray)
    assert img.shape == (100, 120, 3) and info["original_channels"] == 1
    bgra = rng.integers(0, 255, (100, 120, 4), dtype=np.uint8)
    assert validate_array(bgra)[0].shape == (100, 120, 3)
    p = tmp_path / "deep.png"
    cv2.imwrite(str(p), (gray.astype(np.uint16) * 257))
    img16, info16 = load_image(p)
    assert img16.dtype == np.uint8 and info16["original_dtype"] == "uint16"
    assert np.array_equal(img16[..., 0], gray)
