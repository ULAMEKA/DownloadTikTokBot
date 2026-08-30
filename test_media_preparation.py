import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import services.queue as queue

from services.queue import (
    FFMPEG_EXE,
    MediaProbe,
    MissingAudioError,
    NoFastH264FormatError,
    _is_fast_h264_direct,
    _prepare_telegram_video,
)


def _make_video(
    path: Path,
    *,
    video_codec: str,
    audio_codec: str | None,
    fps: int = 30,
) -> None:
    command = [
        FFMPEG_EXE,
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i", f"testsrc2=size=160x90:rate={fps}",
    ]
    if audio_codec is not None:
        command.extend((
            "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=44100",
        ))
    command.extend(("-t", "0.2", "-c:v", video_codec))
    if video_codec == "libx265":
        command.extend((
            "-preset", "ultrafast",
            "-threads", "1",
            "-x265-params", "pools=1:frame-threads=1:log-level=error",
        ))
    else:
        command.extend(("-preset", "ultrafast", "-threads", "1"))
    command.extend(("-pix_fmt", "yuv420p"))
    if audio_codec is None:
        command.append("-an")
    else:
        command.extend(("-c:a", audio_codec, "-shortest"))
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True)


def _assert_video_stream_is_copied(test_case: unittest.TestCase, command: list[str]) -> None:
    test_case.assertEqual(command[command.index("-c:v") + 1], "copy")
    rendered = " ".join(command)
    for forbidden in ("libx264", "-vf", "scale", "-crf", "-fps_mode"):
        test_case.assertNotIn(forbidden, rendered)


@unittest.skipUnless(Path(FFMPEG_EXE).is_file(), "FFmpeg is required")
class MediaPreparationTests(unittest.TestCase):
    def test_h264_aac_mp4_is_used_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            _make_video(source, video_codec="libx264", audio_codec="aac")

            prepared = _prepare_telegram_video(source)

            self.assertEqual(prepared.path, source)
            self.assertEqual(prepared.preparation_mode, "direct")
            self.assertEqual(prepared.probe.video_codec, "h264")
            self.assertEqual(prepared.probe.audio_codec, "aac")
            self.assertTrue(prepared.bypassed_ffmpeg)
            self.assertFalse(source.with_name(f"{source.stem}.telegram.mp4").exists())

    def test_h264_aac_mkv_remuxes_without_video_encoder(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mkv"
            _make_video(source, video_codec="libx264", audio_codec="aac")

            real_run = subprocess.run
            with patch.object(queue.subprocess, "run", wraps=real_run) as run:
                prepared = _prepare_telegram_video(source)

            self.assertEqual(prepared.path, source.with_suffix(".mp4"))
            self.assertEqual(prepared.preparation_mode, "remux_copy")
            self.assertEqual(prepared.probe.video_codec, "h264")
            self.assertEqual(prepared.probe.audio_codec, "aac")
            self.assertFalse(prepared.bypassed_ffmpeg)
            remux_command = next(
                call.args[0]
                for call in run.call_args_list
                if "-c:v" in call.args[0]
            )
            _assert_video_stream_is_copied(self, remux_command)

    def test_h264_mp3_remuxes_with_audio_only_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            _make_video(source, video_codec="libx264", audio_codec="libmp3lame")

            real_run = subprocess.run
            with patch.object(queue.subprocess, "run", wraps=real_run) as run:
                prepared = _prepare_telegram_video(source)

            self.assertEqual(prepared.path, source)
            self.assertEqual(prepared.preparation_mode, "audio_to_aac")
            self.assertFalse(prepared.bypassed_ffmpeg)
            self.assertEqual(prepared.probe.video_codec, "h264")
            self.assertEqual(prepared.probe.audio_codec, "aac")
            command = next(
                call.args[0]
                for call in run.call_args_list
                if "-c:v" in call.args[0]
            )
            _assert_video_stream_is_copied(self, command)
            self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_hevc_source_is_rejected_without_video_transcode(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            _make_video(source, video_codec="libx265", audio_codec="aac")

            real_run = subprocess.run
            with patch.object(queue.subprocess, "run", wraps=real_run) as run:
                with self.assertRaises(NoFastH264FormatError):
                    _prepare_telegram_video(source)

            video_processing_commands = [
                call.args[0]
                for call in run.call_args_list
                if "-c:v" in call.args[0]
            ]
            self.assertEqual(video_processing_commands, [])

    def test_video_without_audio_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "silent.mp4"
            _make_video(source, video_codec="libx264", audio_codec=None)

            with self.assertRaises(MissingAudioError):
                _prepare_telegram_video(source)


class FastH264DirectPredicateTests(unittest.TestCase):
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
            _is_fast_h264_direct(
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
                self.assertFalse(_is_fast_h264_direct(path, probe))


if __name__ == "__main__":
    unittest.main()
