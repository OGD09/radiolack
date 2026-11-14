"""Tests for the recordings: the folder, the names, the WAV files that a
recording's thread writes and why it stops, the list of the recordings,
their deletion and their reading for a download. Every file goes into a
temporary folder, with synthetic audio."""

import errno
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack import recordings
from radiolack.recordings import (AudioRecording, check_folder, chosen_folder, default_folder, delete_recording,
                                  is_recording_name, list_recordings, open_recording, recording_name, wav_header,
                                  wav_seconds)

STARTED = 1_794_000_000.0       # 2026-11-06 21:20:00 UTC
NAME = "2026-11-06_21-20-00Z_145.500000MHz_NFM.wav"
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0     # permissions do not stop root


def tone(seconds: float, rate_hz: int = 12_000) -> bytes:
    """16-bit audio: a 1 kHz tone."""
    t = np.arange(round(seconds * rate_hz)) / rate_hz
    return (np.sin(2 * np.pi * 1000 * t) * 8000).astype("<i2").tobytes()


def write_wav(path: Path, seconds: float, rate_hz: int = 12_000) -> None:
    audio = tone(seconds, rate_hz)
    path.write_bytes(wav_header(rate_hz, len(audio)) + audio)


class Reports:
    """What a recording's thread reported, in order, and when it stopped."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.stopped = threading.Event()

    def __call__(self, _recording: AudioRecording, event: str, value: object) -> None:
        self.events.append((event, value))
        if event == "stopped":
            self.stopped.set()

    def wait(self, test: unittest.TestCase) -> tuple[str | None, str]:
        test.assertTrue(self.stopped.wait(10), "the recording did not stop")
        return self.events[-1][1]

    @property
    def files(self) -> list[str]:
        return [value for event, value in self.events if event == "opened"]


class TemporaryFolder(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)


class FolderTest(TemporaryFolder):
    def test_the_default_folder_follows_xdg_data_home(self):
        self.assertEqual(default_folder({"XDG_DATA_HOME": "/data", "HOME": "/home/user"}),
                         Path("/data/radiolack/recordings"))
        for environ in ({"XDG_DATA_HOME": "relative", "HOME": "/home/user"}, {"HOME": "/home/user"}):
            with self.subTest(environ=environ):
                self.assertEqual(default_folder(environ), Path("/home/user/.local/share/radiolack/recordings"))

    def test_a_chosen_folder_is_a_full_path(self):
        self.assertIsNone(chosen_folder(None))
        self.assertIsNone(chosen_folder(""))
        self.assertEqual(chosen_folder("/media/radio/"), Path("/media/radio"))
        self.assertEqual(chosen_folder(" ~/Music "), Path.home() / "Music")
        for value in ("Music", "./x", 12, True, ["/x"], "/x\0y", "/" + "x" * 5000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                chosen_folder(value)

    def test_checks_that_it_may_write_into_the_folder(self):
        check_folder(self.folder)
        with self.assertRaisesRegex(ValueError, "does not exist$"):
            check_folder(self.folder / "missing")
        (self.folder / "file").write_text("x")
        with self.assertRaisesRegex(ValueError, "is not a folder$"):
            check_folder(self.folder / "file")

    @unittest.skipIf(AS_ROOT, "root writes anywhere")
    def test_refuses_a_folder_it_may_not_write_into(self):
        locked = self.folder / "locked"
        locked.mkdir(mode=0o500)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaisesRegex(ValueError, "^Radiolack may not write into "):
            check_folder(locked)


class NameTest(unittest.TestCase):
    def test_names_tell_the_start_in_utc_the_frequency_and_the_mode(self):
        self.assertEqual(recording_name(STARTED, 145_500_000.0, "NFM"), NAME)
        self.assertEqual(recording_name(STARTED + 0.9, 1_090_000_000.0, "AM", count=2),
                         "2026-11-06_21-20-00Z_1090.000000MHz_AM-2.wav")
        self.assertEqual(recording_name(STARTED, 7_074_250.0, "USB"), "2026-11-06_21-20-00Z_7.074250MHz_USB.wav")

    def test_only_its_own_names_are_recordings(self):
        for name in (NAME, NAME.replace(".wav", "-2.wav"), NAME.replace("NFM", "WFM")):
            with self.subTest(name=name):
                self.assertTrue(is_recording_name(name))
        for name in ("main.js", "../" + NAME, NAME + ".part", NAME.replace("NFM", "FM"),
                     NAME.replace(".wav", "-0.wav"), NAME.replace(".wav", "-1000.wav"), "." + NAME,
                     NAME.replace("Z_", "_"), NAME.replace("2026-11-06", "2026-13-06"),
                     NAME.replace("21-20-00", "24-20-00"), None, 12):
            with self.subTest(name=name):
                self.assertFalse(is_recording_name(name))


class WavTest(unittest.TestCase):
    def test_a_header_gives_the_length(self):
        header = wav_header(12_000, 24_000)
        self.assertEqual(len(header), 44)
        self.assertEqual(wav_seconds(header, 44 + 24_000), 1.0)
        # A crash leaves the header behind the audio, or the audio behind the header.
        self.assertEqual(wav_seconds(wav_header(12_000, 12_000), 44 + 24_000), 0.5)
        self.assertEqual(wav_seconds(header, 44 + 12_000), 0.5)
        self.assertEqual(wav_seconds(wav_header(32_000, 0), 44), 0.0)

    def test_other_files_have_no_length(self):
        header = wav_header(12_000, 24_000)
        for other in (header[:40], b"RIFX" + header[4:], header[:20] + b"\x02\x00" + header[22:],
                      header[:24] + b"\x00\x00\x00\x00" + header[28:]):
            with self.subTest(other=other):
                self.assertIsNone(wav_seconds(other, 44 + 24_000))


class AudioRecordingTest(TemporaryFolder):
    def record(self, folder: Path | None = None, make_folder: bool = True, mode: str = "NFM") -> Reports:
        reports = Reports()
        self.recording = AudioRecording(folder or self.folder, STARTED, 145_500_000.0, mode,
                                        make_folder=make_folder, report=reports)
        self.recording.start()
        self.addCleanup(self.recording.join, 10)
        self.addCleanup(self.recording.stop)
        return reports

    def test_writes_a_wav_file_readable_by_the_user_only(self):
        folder = self.folder / "data" / "radiolack" / "recordings"
        reports = self.record(folder)
        for _ in range(3):
            self.assertTrue(self.recording.write(tone(0.5), 12_000))
        self.recording.stop()
        self.assertEqual(reports.wait(self), (None, ""))
        self.assertEqual(reports.files, [NAME])
        path = folder / NAME
        with wave.open(str(path)) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()),
                             (1, 2, 12_000, 18_000))
            self.assertEqual(wav.readframes(18_000), tone(0.5) * 3)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for made in (folder, folder.parent):
            self.assertEqual(made.stat().st_mode & 0o777, 0o700)

    def test_the_header_follows_each_write(self):
        reports = self.record()
        self.assertTrue(self.recording.write(tone(1.0, 32_000), 32_000))
        path = self.folder / NAME
        deadline = time.monotonic() + 5
        # Read while it records, as after a crash.
        while not (path.exists() and wav_seconds(path.read_bytes()[:44], path.stat().st_size) == 1.0):
            self.assertLess(time.monotonic(), deadline, "the header did not follow the write")
            time.sleep(0.05)
        with wave.open(str(path)) as wav:
            self.assertEqual((wav.getframerate(), wav.getnframes()), (32_000, 32_000))
        self.recording.stop()
        reports.wait(self)

    def test_no_audio_leaves_no_file(self):
        reports = self.record()
        self.recording.stop()
        self.assertEqual(reports.wait(self), (None, ""))
        self.assertEqual(reports.files, [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_name_taken_gets_a_number(self):
        (self.folder / NAME).write_bytes(b"taken")
        reports = self.record()
        self.recording.write(tone(0.1), 12_000)
        self.recording.stop()
        reports.wait(self)
        self.assertEqual(reports.files, [NAME.replace(".wav", "-2.wav")])
        self.assertEqual((self.folder / NAME).read_bytes(), b"taken")

    def test_does_not_start_when_space_is_short(self):
        with mock.patch.object(recordings, "MIN_FREE_BYTES", 2 ** 62):
            reports = self.record()
            self.assertEqual(reports.wait(self), ("space", ""))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_stops_when_space_falls_short(self):
        reports = self.record()
        self.recording.write(tone(0.1), 12_000)
        while not reports.files:
            time.sleep(0.05)
        with mock.patch.object(recordings, "MIN_FREE_BYTES", 2 ** 62):
            self.assertEqual(reports.wait(self), ("space", ""))
        with wave.open(str(self.folder / NAME)) as wav:
            self.assertEqual(wav.getnframes(), 1200)

    def test_stops_once_the_file_is_full(self):
        with mock.patch.object(recordings, "WAV_DATA_LIMIT", 10_000):
            reports = self.record()
            self.recording.write(tone(1.0), 12_000)
            self.assertEqual(reports.wait(self), ("full", ""))
        with wave.open(str(self.folder / NAME)) as wav:
            self.assertEqual(wav.getnframes(), 5_000)

    def test_stops_on_a_disk_error(self):
        reports = self.record()
        failure = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(recordings, "_write_all", side_effect=failure):
            self.recording.write(tone(0.1), 12_000)
            self.assertEqual(reports.wait(self), ("disk", "No space left on device"))

    def test_a_chosen_folder_that_went_stops_it(self):
        reports = self.record(self.folder / "unplugged", make_folder=False)
        self.assertEqual(reports.wait(self), ("disk", "No such file or directory"))
        self.assertFalse((self.folder / "unplugged").exists())

    def test_too_much_waiting_for_the_disk_is_refused(self):
        self.record()
        with mock.patch.object(recordings, "PENDING_LIMIT_BYTES", 30_000):
            self.assertTrue(self.recording.write(tone(1.0), 12_000))
            self.assertFalse(self.recording.write(tone(1.0), 12_000))


class ListTest(TemporaryFolder):
    def test_lists_the_recordings_newest_first(self):
        first, second = NAME, recording_name(STARTED + 60, 433_500_000.0, "AM")
        again = NAME.replace(".wav", "-2.wav")
        for name, seconds in ((first, 1.0), (second, 0.5), (again, 2.0)):
            write_wav(self.folder / name, seconds)
        (self.folder / "notes.txt").write_text("not a recording")
        (self.folder / recording_name(STARTED + 120, 145e6, "NFM")).mkdir()
        os.symlink(self.folder / first, self.folder / recording_name(STARTED + 180, 145e6, "NFM"))
        listed = list_recordings(self.folder, running=second)
        self.assertEqual(listed["files"], [
            {"name": second, "kind": "audio", "started": STARTED + 60, "frequency_hz": 433_500_000, "mode": "AM",
             "bytes": 44 + 12_000, "seconds": 0.5, "running": True},
            {"name": again, "kind": "audio", "started": STARTED, "frequency_hz": 145_500_000, "mode": "NFM",
             "bytes": 44 + 48_000, "seconds": 2.0, "running": False},
            {"name": first, "kind": "audio", "started": STARTED, "frequency_hz": 145_500_000, "mode": "NFM",
             "bytes": 44 + 24_000, "seconds": 1.0, "running": False},
        ])
        self.assertEqual((listed["more"], listed["problem"]), (0, ""))
        self.assertIsInstance(listed["free_bytes"], int)

    def test_lists_the_latest_only(self):
        for minute in range(5):
            write_wav(self.folder / recording_name(STARTED + 60 * minute, 145e6, "NFM"), 0.1)
        listed = list_recordings(self.folder, None, limit=2)
        self.assertEqual([entry["name"] for entry in listed["files"]],
                         [recording_name(STARTED + 240, 145e6, "NFM"), recording_name(STARTED + 180, 145e6, "NFM")])
        self.assertEqual(listed["more"], 3)

    def test_a_file_that_is_not_wav_has_no_length(self):
        (self.folder / NAME).write_bytes(b"not a wav file")
        [entry] = list_recordings(self.folder, None)["files"]
        self.assertEqual((entry["bytes"], entry["seconds"]), (14, None))

    def test_a_folder_not_made_yet_holds_none(self):
        listed = list_recordings(self.folder / "not" / "made", None)
        self.assertEqual((listed["files"], listed["more"], listed["problem"]), ([], 0, ""))
        self.assertIsInstance(listed["free_bytes"], int)    # that of the folders above

    @unittest.skipIf(AS_ROOT, "root reads anywhere")
    def test_tells_when_the_folder_cannot_be_read(self):
        locked = self.folder / "locked"
        locked.mkdir(mode=0o300)
        self.addCleanup(locked.chmod, 0o700)
        listed = list_recordings(locked, None)
        self.assertEqual(listed["files"], [])
        self.assertRegex(listed["problem"], r"cannot be read: Permission denied\.$")


class DeleteAndReadTest(TemporaryFolder):
    def test_deletes_a_recording(self):
        write_wav(self.folder / NAME, 0.1)
        delete_recording(self.folder, NAME)
        self.assertFalse((self.folder / NAME).exists())
        with self.assertRaises(FileNotFoundError):
            delete_recording(self.folder, NAME)

    def test_deletes_nothing_else(self):
        (self.folder / "notes.txt").write_text("kept")
        with self.assertRaises(ValueError):
            delete_recording(self.folder, "notes.txt")
        target = self.folder / "target.wav"
        write_wav(target, 0.1)
        os.symlink(target, self.folder / NAME)
        with self.assertRaises(ValueError):
            delete_recording(self.folder, NAME)
        self.assertTrue(target.exists() and (self.folder / NAME).is_symlink())

    def test_reads_a_recording(self):
        write_wav(self.folder / NAME, 0.5)
        file, size = open_recording(self.folder, NAME)
        with file:
            self.assertEqual(file.read(), (self.folder / NAME).read_bytes())
        self.assertEqual(size, 44 + 12_000)

    def test_reads_nothing_else(self):
        target = self.folder / "secret.txt"
        target.write_text("secret")
        os.symlink(target, self.folder / NAME)
        other = NAME.replace(".wav", "-2.wav")
        os.mkfifo(self.folder / other)      # a named pipe would block a reader without its writer
        for name in (NAME, other, "secret.txt", "../secret.txt", NAME.replace(".wav", "-3.wav")):
            with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                open_recording(self.folder, name)


if __name__ == "__main__":
    unittest.main()
