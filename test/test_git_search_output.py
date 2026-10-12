from io import BytesIO
import unittest

from git_search import (
  ContextBlock,
  CountResult,
  OutputPlan,
  PathResult,
  SearchResult,
  StructuredOutputMode,
  parse_structured_records,
  write_structured_records,
)


class StructuredOutputTest(unittest.TestCase):
  def _parse(self, data, mode):
    return list(parse_structured_records(data, mode))

  def test_match_records(self):
    data = (
      b"module_a.py\0" b"2\0" b"4\0" b"first value\n"
      b"nested\nmodule_b.py\0" b"7\0" b"3\0"
      b"colored \x1b[1;31mvalue\x1b[m\n"
    )
    self.assertEqual(
      self._parse(data, StructuredOutputMode.MATCHES),
      [
        SearchResult("module_a.py", 2, 4, "first value"),
        SearchResult(
          "nested\nmodule_b.py", 7, 3,
          "colored \x1b[1;31mvalue\x1b[m"),
      ],
    )

  def test_context_records_are_grouped_into_blocks(self):
    data = (
      b"module_a.py\0" b"4\0" b"before\n"
      b"module_a.py\0" b"5\0" b"3\0" b"first match\n"
      b"module_a.py\0" b"6\0" b"\n"
      b"\x1b[36m--\x1b[m\n"
      b"module_a.py\0" b"12\0" b"2\0" b"second match\n"
      b"module_b.py\0" b"8\0" b"context\n"
      b"module_b.py\0" b"9\0" b"1\0" b"third match\n"
    )
    blocks = self._parse(data, StructuredOutputMode.CONTEXT)

    self.assertEqual(len(blocks), 3)
    self.assertTrue(all(isinstance(block, ContextBlock) for block in blocks))
    self.assertEqual(blocks[0].path, "module_a.py")
    self.assertEqual(
      blocks[0].lines,
      (
        SearchResult("module_a.py", 4, None, "before"),
        SearchResult("module_a.py", 5, 3, "first match"),
        SearchResult("module_a.py", 6, None, ""),
      ),
    )
    self.assertEqual(
      blocks[0].matches,
      (SearchResult("module_a.py", 5, 3, "first match"),),
    )
    self.assertEqual(blocks[1].path, "module_a.py")
    self.assertEqual(blocks[2].path, "module_b.py")

  def test_path_records(self):
    data = b"nested dir/file:name.py\0line\nbreak.py\0"
    self.assertEqual(
      self._parse(data, StructuredOutputMode.PATHS),
      [
        PathResult("nested dir/file:name.py"),
        PathResult("line\nbreak.py"),
      ],
    )

  def test_count_records(self):
    data = (
      b"module_a.py\0" b"0\n"
      b"module_b.py\0" b"12\n"
      b"nested\nmodule_c.py\0" b"3\n"
    )
    self.assertEqual(
      self._parse(data, StructuredOutputMode.COUNTS),
      [
        CountResult("module_a.py", 0),
        CountResult("module_b.py", 12),
        CountResult("nested\nmodule_c.py", 3),
      ],
    )

  def test_malformed_output_is_rejected(self):
    cases = (
      (b"unterminated.py", StructuredOutputMode.PATHS),
      (b"module.py\0many\n", StructuredOutputMode.COUNTS),
      (b"module.py\0" b"3\0" b"missing column\n",
       StructuredOutputMode.MATCHES),
      (b"module.py\0" b"3\0" b"context only\n",
       StructuredOutputMode.CONTEXT),
      (b"--\n", StructuredOutputMode.CONTEXT),
      (b"3\0" b"2\0" b"text without filename\n",
       StructuredOutputMode.MATCHES),
    )
    for data, mode in cases:
      with self.subTest(data=data, mode=mode):
        with self.assertRaises(ValueError):
          self._parse(data, mode)

  def test_structured_records_are_written_by_mode(self):
    cases = (
      (
        StructuredOutputMode.MATCHES,
        [SearchResult("source.py", 3, 2, "matched value")],
        b"source.py:3:matched value\n",
      ),
      (
        StructuredOutputMode.CONTEXT,
        [ContextBlock("source.py", (
          SearchResult("source.py", 2, None, "before"),
          SearchResult("source.py", 3, 2, "matched value"),
        ))],
        b"source.py-2-before\nsource.py:3:matched value\n",
      ),
      (
        StructuredOutputMode.PATHS,
        [PathResult("source.py")],
        b"source.py\n",
      ),
      (
        StructuredOutputMode.COUNTS,
        [CountResult("source.py", 4)],
        b"source.py:4\n",
      ),
    )
    for mode, records, expected in cases:
      with self.subTest(mode=mode):
        plan = OutputPlan(
          mode=mode,
          show_filename=True,
          show_line_number=True,
          show_column_number=False,
          heading=False,
          break_between_files=False,
          null_terminate_filenames=False,
          color=None,
        )
        stream = BytesIO()
        write_structured_records(records, plan, stream)
        self.assertEqual(stream.getvalue(), expected)


if __name__ == "__main__":
  unittest.main()
