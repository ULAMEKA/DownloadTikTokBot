import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from services.queue import (
    FFMPEG_EXE,
    MediaProbe,
    MissingAudioError,
    _can_bypass_telegram_normalization,
    _prepare_telegram_video,
)


@unittest.skipUnless(Path(FFMPEG_EXE).is_file(), "FFmpeg is required")
class MediaPreparationTests(unittest.TestCase):
    def test_compatible_mp4_is_used_without_ffmpeg_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(
                [
                    FFMPEG_EXE,
                    "-loglevel", "error",
                    "-y",
                    "-f", "lavfi",
                    "-i", "testsrc2=size=160x90:rate=30",
                    "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "0.2",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-threads", "1",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-shortest",
                    str(source),
                ],
                check=True,
                capture_output=True,
            )

            prepared = _prepare_telegram_video(source)

            self.assertEqual(prepared.path, source)
            self.assertEqual(prepared.probe.video_codec, "h264")
            self.assertEqual(prepared.probe.audio_codec, "aac")
            self.assertTrue(prepared.bypassed_ffmpeg)
            self.assertFalse(source.with_name(f"{source.stem}.telegram.mp4").exists())

    def test_hevc_sixty_fps_is_converted_without_losing_fps_or_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mkv"
            subprocess.run(
                [
                    FFMPEG_EXE,
                    "-loglevel", "error",
                    "-y",
                    "-f", "lavfi",
                    "-i", "testsrc2=size=160x90:rate=60",
                    "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "0.5",
                    "-c:v", "libx265",
                    "-preset", "ultrafast",
                    "-threads", "1",
                    "-x265-params", "pools=1:frame-threads=1:log-level=error",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "libmp3lame",
                    "-shortest",
                    str(source),
                ],
                check=True,
                capture_output=True,
            )

            prepared = _prepare_telegram_video(source)

            self.assertEqual(prepared.path, source.with_suffix(".mp4"))
            self.assertEqual(prepared.probe.video_codec, "h264")
            self.assertEqual(prepared.probe.audio_codec, "aac")
            self.assertAlmostEqual(prepared.probe.fps or 0, 60, delta=0.1)
            self.assertTrue(prepared.probe.has_audio)
            self.assertFalse(prepared.bypassed_ffmpeg)

    def test_force_normalize_remuxes_an_otherwise_compatible_mp4(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            subprocess.run(
                [
                    FFMPEG_EXE,
                    "-loglevel", "error",
                    "-y",
                    "-f", "lavfi",
                    "-i", "testsrc2=size=160x90:rate=30",
                    "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "0.2",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-threads", "1",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-shortest",
                    str(source),
                ],
                check=True,
                capture_output=True,
            )
            original_bytes = source.read_bytes()

            prepared = _prepare_telegram_video(source, force_normalize=True)

            self.assertEqual(prepared.path, source)
            self.assertFalse(prepared.bypassed_ffmpeg)
            self.assertEqual(prepared.probe.video_codec, "h264")
            self.assertEqual(prepared.probe.audio_codec, "aac")
            self.assertNotEqual(source.read_bytes(), original_bytes)

    def test_video_without_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "silent.mp4"
            subprocess.run(
                [
                    FFMPEG_EXE,
                    "-loglevel", "error",
                    "-y",
                    "-f", "lavfi",
                    "-i", "color=black:size=160x90:rate=30",
                    "-t", "0.2",
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    "-an",
                    str(source),
                ],
                check=True,
                capture_output=True,
            )

            with self.assertRaises(MissingAudioError):
                _prepare_telegram_video(source)


class TelegramBypassPredicateTests(unittest.TestCase):
    def setUp(self):
        self.compatible_probe = MediaProbe(
            has_video=True,
            has_audio=True,
            video_codec="h264",
            audio_codec="aac",
            pixel_format="yuv420p",
            sample_aspect_ratio="1:1",
        )

    def test_accepts_only_a_compatible_mp4(self):
        self.assertTrue(
            _can_bypass_telegram_normalization(
                Path("video.mp4"),
                self.compatible_probe,
            )
        )

    def test_rejects_wrong_suffix_audio_rotation_and_sar(self):
        incompatible_cases = (
            (Path("video.mkv"), self.compatible_probe),
            (
                Path("video.mp4"),
                replace(self.compatible_probe, audio_codec="mp3"),
            ),
            (
                Path("video.mp4"),
                replace(self.compatible_probe, rotation=90),
            ),
            (
                Path("video.mp4"),
                replace(
                    self.compatible_probe,
                    sample_aspect_ratio="4:3",
                ),
            ),
        )

        for path, probe in incompatible_cases:
            with self.subTest(path=path, probe=probe):
                self.assertFalse(_can_bypass_telegram_normalization(path, probe))


if __name__ == "__main__":
    unittest.main()
