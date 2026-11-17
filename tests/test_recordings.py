"""Tests for the recordings: the folder, the names, the WAV and SigMF
files that a recording's thread writes and why it stops, the list of the
recordings, their deletion and their reading for a download. Every file
goes into a temporary folder, with synthetic audio and I/Q."""

import errno
import json
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from radiolack import __version__, recordings
from radiolack.recordings import (AudioRecording, IQRecording, check_folder, chosen_folder, default_folder,
                                  delete_recording, iq_name, is_recording_file, is_recording_name, list_recordings,
                                  meta_name, open_recording, recording_name, wav_header, wav_seconds)

STARTED = 1_794_000_000.0       # 2026-11-06 21:20:00 UTC
NAME = "2026-11-06_21-20-00Z_145.500000MHz_NFM.wav"
IQ_NAME = "2026-11-06_21-20-00Z_145.000000MHz_2.400000MSps.sigmf-data"
IQ_META = "2026-11-06_21-20-00Z_145.000000MHz_2.400000MSps.sigmf-meta"
IQ_RATE = 2_400_000.0
AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0     # permissions do not stop root


def tone(seconds: float, rate_hz: int = 12_000) -> bytes:
    """16-bit audio: a 1 kHz tone."""
    t = np.arange(round(seconds * rate_hz)) / rate_hz
    return (np.sin(2 * np.pi * 1000 * t) * 8000).astype("<i2").tobytes()


def write_wav(path: Path, seconds: float, rate_hz: int = 12_000) -> None:
    audio = tone(seconds, rate_hz)
    path.write_bytes(wav_header(rate_hz, len(audio)) + audio)


def iq(pairs: int, code: int = 0x81) -> bytes:
    """Raw I/Q as rtl_tcp sends it: ``pairs`` I/Q pairs of unsigned bytes,
    whose I byte tells them apart."""
    return bytes((code, 0x7E)) * pairs


class Reports:
    """What a recording's thread reported, in order, and when it stopped."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.stopped = threading.Event()

    def __call__(self, _recording: recordings.Recording, event: str, value: object) -> None:
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
        self.assertEqual(recording_name(STARTED, 9_420_000.0, "SAM"), "2026-11-06_21-20-00Z_9.420000MHz_SAM.wav")

    def test_only_its_own_names_are_recordings(self):
        for name in (NAME, NAME.replace(".wav", "-2.wav"), NAME.replace("NFM", "WFM"), NAME.replace("NFM", "SAM"),
                     NAME.replace("NFM", "AM").replace(".wav", "-3.wav")):
            with self.subTest(name=name):
                self.assertTrue(is_recording_name(name))
        for name in ("main.js", "../" + NAME, NAME + ".part", NAME.replace("NFM", "FM"), NAME.replace("NFM", "SSAM"),
                     NAME.replace(".wav", "-0.wav"), NAME.replace(".wav", "-1000.wav"), "." + NAME,
                     NAME.replace("Z_", "_"), NAME.replace("2026-11-06", "2026-13-06"),
                     NAME.replace("21-20-00", "24-20-00"), None, 12):
            with self.subTest(name=name):
                self.assertFalse(is_recording_name(name))

    def test_names_an_iq_recording_after_the_centre_frequency_and_the_sample_rate(self):
        self.assertEqual(iq_name(STARTED, 145e6, IQ_RATE), IQ_NAME)
        self.assertEqual(iq_name(STARTED, 433_920_000.0, 1_024_000.0, count=3),
                         "2026-11-06_21-20-00Z_433.920000MHz_1.024000MSps-3.sigmf-data")
        self.assertEqual(meta_name(IQ_NAME), IQ_META)
        with self.assertRaises(ValueError):
            meta_name(NAME)

    def test_an_iq_recording_is_its_data_file_and_its_metadata_downloads_too(self):
        self.assertTrue(is_recording_name(IQ_NAME))
        self.assertFalse(is_recording_name(IQ_META))    # listed and deleted with its data
        for name in (NAME, IQ_NAME, IQ_META, IQ_META.replace(".sigmf-meta", "-2.sigmf-meta")):
            with self.subTest(name=name):
                self.assertTrue(is_recording_file(name))
        for name in (IQ_NAME.replace("2.400000", "0.000000"), IQ_NAME.replace("2.400000", "2.4"),
                     IQ_NAME.replace(".sigmf-data", ".sigmf"), IQ_NAME.replace(".sigmf-data", ".sigmf-data.tmp"),
                     "." + IQ_META, "../" + IQ_META, IQ_NAME.replace("MSps", "MHz"), "notes.txt", None):
            with self.subTest(name=name):
                self.assertFalse(is_recording_file(name))


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


class IQRecordingTest(TemporaryFolder):
    def record(self, folder: Path | None = None, make_folder: bool = True) -> Reports:
        reports = Reports()
        self.recording = IQRecording(folder or self.folder, STARTED, 145e6, IQ_RATE, hardware="RTL-SDR",
                                     make_folder=make_folder, report=reports)
        self.recording.start()
        self.addCleanup(self.recording.join, 10)
        self.addCleanup(self.recording.stop)
        return reports

    def captures(self) -> list[dict] | None:
        """The captures of the metadata as it is now, or None before it is written."""
        try:
            return json.loads((self.folder / IQ_META).read_text(encoding="ascii"))["captures"]
        except FileNotFoundError:
            return None

    def wait_for(self, condition) -> None:
        deadline = time.monotonic() + 5
        while not condition():
            self.assertLess(time.monotonic(), deadline, "the condition never held")
            time.sleep(0.02)

    def test_writes_the_samples_and_their_metadata_readable_by_the_user_only(self):
        folder = self.folder / "data" / "radiolack" / "recordings"
        reports = self.record(folder)
        self.recording.hardware = "RTL-SDR, Rafael Micro R820T"    # once rtl_tcp named its tuner
        when = STARTED + 10
        self.assertTrue(self.recording.write(iq(1200), 145e6, when, False))
        self.assertTrue(self.recording.write(iq(1200), 145e6, when + 0.001, True))
        self.assertTrue(self.recording.write(iq(2400, 0x90), 433.92e6, when + 2, False))   # retuned
        self.assertTrue(self.recording.write(iq(240, 0x90), 433.92e6, when + 3, False))    # after a gap
        self.recording.stop()
        self.assertEqual(reports.wait(self), (None, ""))
        self.assertEqual(reports.files, [IQ_NAME])
        self.assertEqual((folder / IQ_NAME).read_bytes(), iq(2400) + iq(2640, 0x90))
        self.assertEqual(json.loads((folder / IQ_META).read_text(encoding="ascii")), {
            "global": {"core:datatype": "cu8", "core:sample_rate": 2_400_000, "core:version": "1.0.0",
                       "core:hw": "RTL-SDR, Rafael Micro R820T", "core:recorder": f"Radiolack {__version__}"},
            # Each capture starts with its first sample: its chunk's length before the chunk came.
            "captures": [
                {"core:sample_start": 0, "core:frequency": 145_000_000, "core:datetime": "2026-11-06T21:20:09.999Z"},
                {"core:sample_start": 2400, "core:frequency": 433_920_000,
                 "core:datetime": "2026-11-06T21:20:11.999Z"},
                {"core:sample_start": 4800, "core:frequency": 433_920_000,
                 "core:datetime": "2026-11-06T21:20:12.999Z"},
            ],
            "annotations": [],
        })
        for path in (folder / IQ_NAME, folder / IQ_META):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [IQ_NAME, IQ_META])   # nothing else

    def test_the_metadata_follows_the_captures_while_it_records(self):
        with mock.patch.object(recordings, "META_INTERVAL_S", 0.0):
            reports = self.record()
            self.recording.write(iq(100), 145e6, STARTED, False)
            # Read while it records, as after a crash.
            self.wait_for(lambda: self.captures() is not None)
            self.assertEqual([capture["core:frequency"] for capture in self.captures()], [145_000_000])
            self.recording.write(iq(100), 146e6, STARTED + 1, False)
            self.wait_for(lambda: len(self.captures()) == 2)
            self.recording.stop()
            reports.wait(self)
        self.assertEqual([capture["core:sample_start"] for capture in self.captures()], [0, 100])

    def test_the_metadata_is_written_at_most_every_interval_and_at_the_end(self):
        with mock.patch.object(recordings, "META_INTERVAL_S", 3600.0):
            reports = self.record()
            self.recording.write(iq(100), 145e6, STARTED, False)
            self.wait_for(lambda: self.captures() is not None)      # the first, at once
            self.recording.write(iq(100), 146e6, STARTED + 1, False)
            time.sleep(3 * recordings.WRITE_INTERVAL_S)
            self.assertEqual(len(self.captures()), 1)
            self.recording.stop()
            reports.wait(self)
        self.assertEqual([capture["core:frequency"] for capture in self.captures()], [145_000_000, 146_000_000])

    def test_no_samples_leave_no_file(self):
        reports = self.record()
        self.recording.stop()
        self.assertEqual(reports.wait(self), (None, ""))
        self.assertEqual(reports.files, [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_name_taken_by_either_file_gets_a_number(self):
        (self.folder / IQ_NAME).write_bytes(b"taken")
        lone = IQ_META.replace(".sigmf-meta", "-2.sigmf-meta")     # metadata whose data went
        (self.folder / lone).write_text("{}")
        reports = self.record()
        self.recording.write(iq(10), 145e6, STARTED, False)
        self.recording.stop()
        reports.wait(self)
        self.assertEqual(reports.files, [IQ_NAME.replace(".sigmf-data", "-3.sigmf-data")])
        self.assertEqual(((self.folder / IQ_NAME).read_bytes(), (self.folder / lone).read_text()), (b"taken", "{}"))

    def test_does_not_start_when_space_is_short(self):
        with mock.patch.object(recordings, "MIN_FREE_BYTES", 2 ** 62):
            reports = self.record()
            self.assertEqual(reports.wait(self), ("space", ""))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_stops_when_space_falls_short_with_its_metadata(self):
        reports = self.record()
        self.recording.write(iq(100), 145e6, STARTED, False)
        while not reports.files:
            time.sleep(0.05)
        with mock.patch.object(recordings, "MIN_FREE_BYTES", 2 ** 62):
            self.assertEqual(reports.wait(self), ("space", ""))
        self.assertEqual((self.folder / IQ_NAME).read_bytes(), iq(100))
        self.assertEqual(len(self.captures()), 1)

    def test_stops_on_a_disk_error(self):
        reports = self.record()
        failure = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(recordings, "_write_all", side_effect=failure):
            self.recording.write(iq(10), 145e6, STARTED, False)
            self.assertEqual(reports.wait(self), ("disk", "No space left on device"))
        self.assertEqual([path.name for path in self.folder.iterdir()], [IQ_NAME])      # no metadata, no temporary file

    def test_too_much_waiting_for_the_disk_is_refused(self):
        self.record()
        with mock.patch.object(recordings, "IQ_PENDING_LIMIT_BYTES", 30_000):
            self.assertTrue(self.recording.write(iq(15_000), 145e6, STARTED, False))
            self.assertFalse(self.recording.write(iq(1), 145e6, STARTED, True))


class ListTest(TemporaryFolder):
    def test_lists_the_recordings_newest_first(self):
        first, second = NAME, recording_name(STARTED + 60, 9_420_000.0, "SAM")
        again = NAME.replace(".wav", "-2.wav")
        for name, seconds in ((first, 1.0), (second, 0.5), (again, 2.0)):
            write_wav(self.folder / name, seconds)
        (self.folder / "notes.txt").write_text("not a recording")
        (self.folder / recording_name(STARTED + 120, 145e6, "NFM")).mkdir()
        os.symlink(self.folder / first, self.folder / recording_name(STARTED + 180, 145e6, "NFM"))
        listed = list_recordings(self.folder, running={second})
        self.assertEqual(listed["files"], [
            {"name": second, "kind": "audio", "started": STARTED + 60, "frequency_hz": 9_420_000, "mode": "SAM",
             "bytes": 44 + 12_000, "seconds": 0.5, "running": True},
            {"name": again, "kind": "audio", "started": STARTED, "frequency_hz": 145_500_000, "mode": "NFM",
             "bytes": 44 + 48_000, "seconds": 2.0, "running": False},
            {"name": first, "kind": "audio", "started": STARTED, "frequency_hz": 145_500_000, "mode": "NFM",
             "bytes": 44 + 24_000, "seconds": 1.0, "running": False},
        ])
        self.assertEqual((listed["more"], listed["problem"]), (0, ""))
        self.assertIsInstance(listed["free_bytes"], int)

    def test_lists_an_iq_recording_as_its_data_with_its_metadata(self):
        write_wav(self.folder / NAME, 1.0)
        (self.folder / IQ_NAME).write_bytes(iq(240_000))       # 0.1 s
        (self.folder / IQ_META).write_text("{}")
        other = iq_name(STARTED + 60, 433_920_000.0, 1_024_000.0)
        (self.folder / other).write_bytes(iq(1_024_000))       # 1 s, without its metadata
        (self.folder / meta_name(iq_name(STARTED + 120, 145e6, IQ_RATE))).write_text("{}")     # without its data
        listed = list_recordings(self.folder, running={other, meta_name(other)})
        self.assertEqual(listed["files"], [
            {"name": other, "kind": "iq", "started": STARTED + 60, "frequency_hz": 433_920_000, "mode": "I/Q",
             "bytes": 2_048_000, "seconds": 1.0, "running": True, "rate_hz": 1_024_000, "meta": None},
            {"name": NAME, "kind": "audio", "started": STARTED, "frequency_hz": 145_500_000, "mode": "NFM",
             "bytes": 44 + 24_000, "seconds": 1.0, "running": False},
            {"name": IQ_NAME, "kind": "iq", "started": STARTED, "frequency_hz": 145_000_000, "mode": "I/Q",
             "bytes": 480_000 + 2, "seconds": 0.1, "running": False, "rate_hz": 2_400_000, "meta": IQ_META},
        ])

    def test_lists_the_latest_only(self):
        for minute in range(5):
            write_wav(self.folder / recording_name(STARTED + 60 * minute, 145e6, "NFM"), 0.1)
        listed = list_recordings(self.folder, limit=2)
        self.assertEqual([entry["name"] for entry in listed["files"]],
                         [recording_name(STARTED + 240, 145e6, "NFM"), recording_name(STARTED + 180, 145e6, "NFM")])
        self.assertEqual(listed["more"], 3)

    def test_a_file_that_is_not_wav_has_no_length(self):
        (self.folder / NAME).write_bytes(b"not a wav file")
        [entry] = list_recordings(self.folder)["files"]
        self.assertEqual((entry["bytes"], entry["seconds"]), (14, None))

    def test_a_folder_not_made_yet_holds_none(self):
        listed = list_recordings(self.folder / "not" / "made")
        self.assertEqual((listed["files"], listed["more"], listed["problem"]), ([], 0, ""))
        self.assertIsInstance(listed["free_bytes"], int)    # that of the folders above

    @unittest.skipIf(AS_ROOT, "root reads anywhere")
    def test_tells_when_the_folder_cannot_be_read(self):
        locked = self.folder / "locked"
        locked.mkdir(mode=0o300)
        self.addCleanup(locked.chmod, 0o700)
        listed = list_recordings(locked)
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

    def test_deletes_an_iq_recording_with_its_metadata(self):
        (self.folder / IQ_NAME).write_bytes(iq(10))
        (self.folder / IQ_META).write_text("{}")
        with self.assertRaises(ValueError):
            delete_recording(self.folder, IQ_META)      # it goes with its data
        delete_recording(self.folder, IQ_NAME)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_an_iq_recording_without_its_metadata_and_a_link_in_its_place(self):
        (self.folder / IQ_NAME).write_bytes(iq(10))
        delete_recording(self.folder, IQ_NAME)          # without its metadata
        self.assertEqual(list(self.folder.iterdir()), [])
        target = self.folder / "target.json"
        target.write_text("{}")
        (self.folder / IQ_NAME).write_bytes(iq(10))
        os.symlink(target, self.folder / IQ_META)
        delete_recording(self.folder, IQ_NAME)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [IQ_META, "target.json"])

    def test_reads_an_iq_recording_and_its_metadata(self):
        for name, content in ((IQ_NAME, iq(10)), (IQ_META, b"{}")):
            (self.folder / name).write_bytes(content)
            file, size = open_recording(self.folder, name)
            with file:
                self.assertEqual((file.read(), size), (content, len(content)))

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
