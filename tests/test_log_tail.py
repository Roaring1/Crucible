import tempfile
import unittest
from pathlib import Path

from crucible.process.log_tail import LogTailReader


class LogTailReaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.log = self.root / "latest.log"
        self.log.write_bytes(b"")

    def test_incomplete_line_is_buffered_then_emitted_once(self):
        reader = LogTailReader()
        self.log.write_bytes(b"alpha\nbeta")
        first = reader.read(self.log, chunk_bytes=2)
        self.assertEqual(first.lines, ["alpha"])
        self.assertEqual(reader.buffered_bytes, 4)
        with self.log.open("ab") as fh:
            fh.write(b" gamma\n")
        second = reader.read(self.log, chunk_bytes=3)
        self.assertEqual(second.lines, ["beta gamma"])
        self.assertEqual(reader.buffered_bytes, 0)

    def test_multibyte_utf8_split_across_polls_is_not_corrupted(self):
        reader = LogTailReader()
        value = "Player joined: 🐉"
        encoded = value.encode("utf-8")
        self.log.write_bytes(encoded[:-2])
        self.assertEqual(reader.read(self.log).lines, [])
        with self.log.open("ab") as fh:
            fh.write(encoded[-2:] + b"\n")
        self.assertEqual(reader.read(self.log).lines, [value])

    def test_burst_is_capped_and_drained_in_multiple_reads(self):
        reader = LogTailReader()
        self.log.write_bytes(b"one\ntwo\nthree\nfour\n")
        first = reader.read(self.log, max_read_bytes=8, chunk_bytes=3)
        self.assertEqual(first.bytes_read, 8)
        self.assertTrue(first.backlog)
        lines = first.lines
        while True:
            result = reader.read(self.log, max_read_bytes=8, chunk_bytes=3)
            lines.extend(result.lines)
            if not result.backlog:
                break
        self.assertEqual(lines, ["one", "two", "three", "four"])

    def test_truncation_resets_position_and_partial_state(self):
        reader = LogTailReader()
        self.log.write_bytes(b"old complete\nold partial")
        self.assertEqual(reader.read(self.log).lines, ["old complete"])
        self.log.write_bytes(b"new complete\n")
        result = reader.read(self.log)
        self.assertTrue(result.rotated)
        self.assertEqual(result.lines, ["new complete"])

    def test_replacement_inode_is_rotation_even_when_larger(self):
        reader = LogTailReader()
        self.log.write_bytes(b"old\n")
        reader.read(self.log)
        replacement = self.root / "replacement.log"
        replacement.write_bytes(b"new first\nnew second\n")
        replacement.replace(self.log)
        result = reader.read(self.log)
        self.assertTrue(result.rotated)
        self.assertEqual(result.lines, ["new first", "new second"])

    def test_unterminated_line_buffer_is_bounded(self):
        reader = LogTailReader(max_partial_bytes=128)
        self.log.write_bytes(b"x" * 4096)
        result = reader.read(self.log, max_read_bytes=4096, chunk_bytes=31)
        self.assertEqual(result.lines, [])
        self.assertLessEqual(reader.buffered_bytes, 128)
        with self.log.open("ab") as fh:
            fh.write(b"\n")
        completed = reader.read(self.log)
        self.assertEqual(len(completed.lines), 1)
        self.assertIn("oversized log line omitted", completed.lines[0])

    def test_invalid_utf8_is_replaced_not_fatal(self):
        reader = LogTailReader()
        self.log.write_bytes(b"before \xff after\n")
        self.assertEqual(reader.read(self.log).lines, ["before � after"])


if __name__ == "__main__":
    unittest.main()
