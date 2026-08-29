import subprocess
import tempfile
import unittest
from pathlib import Path

from services.queue import (
    FFMPEG_EXE,
    MissingAudioError,
    _prepare_telegram_video,
)


@unittest.skipUnless(Path(FFMPEG_EXE).is_file(), "FFmpeg is required")
class MediaPreparationTests(unittest.TestCase):
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

            final, probe = _prepare_telegram_video(source)

            self.assertEqual(final, source.with_suffix(".mp4"))
            self.assertEqual(probe.video_codec, "h264")
            self.assertEqual(probe.audio_codec, "aac")
            self.assertAlmostEqual(probe.fps or 0, 60, delta=0.1)
            self.assertTrue(probe.has_audio)

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


if __name__ == "__main__":
    unittest.main()
