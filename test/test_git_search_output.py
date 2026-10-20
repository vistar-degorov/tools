from io import BytesIO
import os
import unittest

from lib.git_search_args import OutputPlan
from lib.git_search import (
  ContextBlock,
  CountResult,
  PathResult,
  SearchResult,
  GrepOutputMode,
  build_command,
  parse_grep_results,
  write_records,
)


class TerminalBytesIO(BytesIO):
  def isatty(self):
    return True


class StructuredOutputTest(unittest.TestCase):
  def _parse(self, data, mode):
    return list(parse_grep_results(data, mode))

  def _render(self, records, mode, *, terminal=False, **overrides):
    settings = {
      "show_filename": True,
      "show_line_number": True,
      "show_column_number": False,
      "heading": False,
      "break_between_files": False,
      "null_terminate_filenames": False,
      "color": None,
    }
    settings.update(overrides)
    stream = TerminalBytesIO() if terminal else BytesIO()
    write_records(records, OutputPlan(mode=mode, **settings), stream)
    return stream.getvalue()

  def test_match_records(self):
    data = (
      b"module_a.py\0" b"2\0" b"4\0" b"first value\n"
      b"nested\nmodule_b.py\0" b"7\0" b"3\0"
      b"colored \x1b[1;31mvalue\x1b[m\n"
    )
    self.assertEqual(
      self._parse(data, GrepOutputMode.MATCHES),
      [
        SearchResult("module_a.py", 2, 4, b"first value"),
        SearchResult(
          "nested\nmodule_b.py", 7, 3,
          b"colored \x1b[1;31mvalue\x1b[m"),
      ],
    )

  def test_match_text_round_trips_as_bytes(self):
    data = b"source.py\0" b"3\0" b"7\0" b"raw-\xff-value\n"
    records = self._parse(data, GrepOutputMode.MATCHES)
    self.assertEqual(
      records,
      [SearchResult("source.py", 3, 7, b"raw-\xff-value")],
    )
    self.assertEqual(self._render(records, GrepOutputMode.MATCHES), b"source.py:3:raw-\xff-value\n")

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
    blocks = self._parse(data, GrepOutputMode.CONTEXT)

    self.assertEqual(len(blocks), 3)
    self.assertTrue(all(isinstance(block, ContextBlock) for block in blocks))
    self.assertEqual(blocks[0].path, "module_a.py")
    self.assertEqual(
      blocks[0].lines,
      (
        SearchResult("module_a.py", 4, None, b"before"),
        SearchResult("module_a.py", 5, 3, b"first match"),
        SearchResult("module_a.py", 6, None, b""),
      ),
    )
    self.assertEqual(
      blocks[0].matches,
      (SearchResult("module_a.py", 5, 3, b"first match"),),
    )
    self.assertEqual(blocks[1].path, "module_a.py")
    self.assertEqual(blocks[2].path, "module_b.py")

  def test_path_records(self):
    data = b"nested dir/file:name.py\0line\nbreak.py\0"
    self.assertEqual(
      self._parse(data, GrepOutputMode.PATHS),
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
      self._parse(data, GrepOutputMode.COUNTS),
      [
        CountResult("module_a.py", 0),
        CountResult("module_b.py", 12),
        CountResult("nested\nmodule_c.py", 3),
      ],
    )

  def test_malformed_output_is_rejected(self):
    cases = (
      (b"unterminated.py", GrepOutputMode.PATHS),
      (b"module.py\0many\n", GrepOutputMode.COUNTS),
      (b"module.py\0" b"3\0" b"missing column\n",
       GrepOutputMode.MATCHES),
      (b"module.py\0" b"3\0" b"context only\n",
       GrepOutputMode.CONTEXT),
      (b"--\n", GrepOutputMode.CONTEXT),
      (b"3\0" b"2\0" b"text without filename\n",
       GrepOutputMode.MATCHES),
    )
    for data, mode in cases:
      with self.subTest(data=data, mode=mode):
        with self.assertRaises(ValueError):
          self._parse(data, mode)

  def test_structured_records_are_written_by_mode(self):
    cases = (
      (
        GrepOutputMode.MATCHES,
        [SearchResult("source.py", 3, 2, b"matched value")],
        b"source.py:3:matched value\n",
      ),
      (
        GrepOutputMode.CONTEXT,
        [ContextBlock("source.py", (
          SearchResult("source.py", 2, None, b"before"),
          SearchResult("source.py", 3, 2, b"matched value"),
        ))],
        b"source.py-2-before\nsource.py:3:matched value\n",
      ),
      (
        GrepOutputMode.PATHS,
        [PathResult("source.py")],
        b"source.py\n",
      ),
      (
        GrepOutputMode.COUNTS,
        [CountResult("source.py", 4)],
        b"source.py:4\n",
      ),
    )
    for mode, records, expected in cases:
      with self.subTest(mode=mode):
        self.assertEqual(self._render(records, mode), expected)

  def test_match_fields_follow_the_output_plan(self):
    result = SearchResult("nested/source.py", 12, 4, b"matched value")
    cases = (
      ({}, b"nested/source.py:12:matched value\n"),
      (
        {"show_column_number": True},
        b"nested/source.py:12:4:matched value\n",
      ),
      (
        {"show_filename": False},
        b"12:matched value\n",
      ),
      (
        {
          "show_filename": False,
          "show_line_number": False,
          "show_column_number": True,
        },
        b"4:matched value\n",
      ),
      (
        {
          "show_filename": False,
          "show_line_number": False,
        },
        b"matched value\n",
      ),
      (
        {
          "show_column_number": True,
          "null_terminate_filenames": True,
        },
        b"nested/source.py\0" b"12\0" b"4\0" b"matched value\n",
      ),
    )
    for settings, expected in cases:
      with self.subTest(settings=settings):
        self.assertEqual(
          self._render([result], GrepOutputMode.MATCHES, **settings),
          expected,
        )

  def test_match_headings_and_file_breaks(self):
    records = (
      SearchResult("first.py", 2, 1, b"first match"),
      SearchResult("first.py", 4, 2, b"second match"),
      SearchResult("second.py", 6, 3, b"third match"),
    )
    self.assertEqual(
      self._render(
        records,
        GrepOutputMode.MATCHES,
        show_column_number=True,
        heading=True,
        break_between_files=True,
      ),
      (
        b"first.py\n"
        b"2:1:first match\n"
        b"4:2:second match\n"
        b"\n"
        b"second.py\n"
        b"6:3:third match\n"
      ),
    )

  def test_heading_remains_newline_terminated_in_null_output(self):
    result = SearchResult("source.py", 12, 4, b"matched value")
    self.assertEqual(
      self._render(
        [result],
        GrepOutputMode.MATCHES,
        show_column_number=True,
        heading=True,
        null_terminate_filenames=True,
      ),
      b"source.py\n" b"12\0" b"4\0" b"matched value\n",
    )

  def test_context_blocks_use_context_and_block_separators(self):
    blocks = (
      ContextBlock("first.py", (
        SearchResult("first.py", 2, None, b"before"),
        SearchResult("first.py", 3, 2, b"first match"),
      )),
      ContextBlock("first.py", (
        SearchResult("first.py", 8, 4, b"second match"),
      )),
      ContextBlock("second.py", (
        SearchResult("second.py", 5, 1, b"third match"),
        SearchResult("second.py", 6, None, b"after"),
      )),
    )
    self.assertEqual(
      self._render(
        blocks,
        GrepOutputMode.CONTEXT,
        show_column_number=True,
        heading=True,
        break_between_files=True,
      ),
      (
        b"first.py\n"
        b"2-before\n"
        b"3:2:first match\n"
        b"--\n"
        b"8:4:second match\n"
        b"\n"
        b"second.py\n"
        b"5:1:third match\n"
        b"6-after\n"
      ),
    )

  def test_context_null_output_separates_every_visible_field(self):
    block = ContextBlock("source.py", (
      SearchResult("source.py", 2, None, b"before"),
      SearchResult("source.py", 3, 5, b"matched value"),
    ))
    self.assertEqual(
      self._render(
        [block],
        GrepOutputMode.CONTEXT,
        show_column_number=True,
        null_terminate_filenames=True,
      ),
      (
        b"source.py\0" b"2\0" b"before\n"
        b"source.py\0" b"3\0" b"5\0" b"matched value\n"
      ),
    )

  def test_path_and_count_terminators(self):
    path = "nested\nsource.py"
    self.assertEqual(
      self._render(
        [PathResult(path)],
        GrepOutputMode.PATHS,
        null_terminate_filenames=True,
      ),
      b"nested\nsource.py\0",
    )
    self.assertEqual(
      self._render(
        [CountResult(path, 7)],
        GrepOutputMode.COUNTS,
        null_terminate_filenames=True,
      ),
      b"nested\nsource.py\0" b"7\n",
    )
    self.assertEqual(
      self._render(
        [CountResult(path, 7)],
        GrepOutputMode.COUNTS,
        show_filename=False,
      ),
      b"7\n",
    )

  def test_paths_use_the_filesystem_encoding(self):
    encoded_path = b"source-\xff.py"
    path = os.fsdecode(encoded_path)
    self.assertEqual(
      self._render(
        [PathResult(path)],
        GrepOutputMode.PATHS,
        null_terminate_filenames=True,
      ),
      encoded_path + b"\0",
    )

  def test_color_follows_the_plan_and_output_destination(self):
    result = SearchResult(
      "source.py", 3, 2, b"\x1b[1;31mmatched\x1b[m value")
    plain = b"source.py:3:\x1b[1;31mmatched\x1b[m value\n"
    colored = (
      b"source.py\x1b[36m:\x1b[m"
      b"3\x1b[36m:\x1b[m"
      b"\x1b[1;31mmatched\x1b[m value\n"
    )
    cases = (
      ({"color": "always"}, False, colored),
      ({"color": "never"}, True, plain),
      ({"color": "auto"}, False, plain),
      ({"color": "auto"}, True, colored),
      ({"color": None}, False, plain),
      ({"color": None}, True, colored),
    )
    for settings, terminal, expected in cases:
      with self.subTest(settings=settings, terminal=terminal):
        self.assertEqual(
          self._render(
            [result], GrepOutputMode.MATCHES,
            terminal=terminal, **settings),
          expected,
        )

  def test_context_block_separator_is_colored(self):
    blocks = (
      ContextBlock("source.py", (
        SearchResult("source.py", 2, 1, b"first match"),
      )),
      ContextBlock("source.py", (
        SearchResult("source.py", 5, 1, b"second match"),
      )),
    )
    self.assertEqual(
      self._render(
        blocks, GrepOutputMode.CONTEXT,
        show_filename=False, color="always"),
      (
        b"2\x1b[36m:\x1b[mfirst match\n"
        b"\x1b[36m--\x1b[m\n"
        b"5\x1b[36m:\x1b[msecond match\n"
      ),
    )

  def test_null_separators_are_not_colored(self):
    result = SearchResult("source.py", 3, 2, b"matched value")
    self.assertEqual(
      self._render(
        [result], GrepOutputMode.MATCHES,
        show_column_number=True,
        null_terminate_filenames=True,
        color="always",
      ),
      b"source.py\0" b"3\0" b"2\0" b"matched value\n",
    )

  def test_structured_command_can_preserve_match_color(self):
    command = build_command(
      ("needle",), (), ("--", "*.py"),
      color=True)
    self.assertIn("--color=always", command)
    self.assertNotIn("--color=never", command)


if __name__ == "__main__":
  unittest.main()
