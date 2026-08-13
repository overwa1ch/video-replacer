#!/usr/bin/env python3
"""Platform-neutral tests for native Windows mosaic support."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "privacy" / "face_mosaic.py"
SPEC = importlib.util.spec_from_file_location("face_mosaic_windows_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
mosaic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mosaic)


class WindowsMosaicTests(unittest.TestCase):
    def test_native_windows_ffmpeg_shim_is_regular_exe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "imageio-ffmpeg-win64.exe"
            target.write_bytes(b"reviewed ffmpeg fixture")
            shim = root / mosaic.ffmpeg_shim_name("nt")
            mosaic.materialize_ffmpeg_shim(target, shim, "nt")
            self.assertEqual(shim.name, "ffmpeg.exe")
            self.assertTrue(shim.is_file())
            self.assertFalse(shim.is_symlink())
            self.assertEqual(shim.read_bytes(), target.read_bytes())


if __name__ == "__main__":
    unittest.main()
