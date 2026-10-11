from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys


@dataclass(frozen=True)
class SearchResult:
  path: str
  line: int | None
  column: int | None
  text: str

  @property
  def is_match(self):
    return self.line is not None and self.column is not None


@dataclass(frozen=True)
class ContextBlock:
  path: str
  lines: tuple[SearchResult, ...]

  def __post_init__(self):
    if not self.lines:
      raise ValueError("context block is empty")
    if any(line.path != self.path for line in self.lines):
      raise ValueError("context block contains multiple paths")
    if any(line.line is None for line in self.lines):
      raise ValueError("context block contains a record without a line")
    if not self.matches:
      raise ValueError("context block contains no matches")

  @property
  def matches(self):
    return tuple(line for line in self.lines if line.is_match)


@dataclass(frozen=True)
class PathResult:
  path: str


@dataclass(frozen=True)
class CountResult:
  path: str
  count: int


class StructuredOutputMode(Enum):
  MATCHES = "matches"
  CONTEXT = "context"
  PATHS = "paths"
  COUNTS = "counts"


def remove_color_codes(text):
  return re.sub(r"\x1b\[[0-9;]*m", "", text)


def build_command(
  patterns,
  grep_args,
  scope_arguments,
  *,
  structured=False,
  untracked=False,
  word_regexp=False,
):
  cmd = ["git", "grep"]
  if untracked:
    cmd.extend(("--untracked", "--exclude-standard"))
  if structured:
    cmd.extend(("--color=never", "-I", "-n", "--column", "-z"))
  else:
    cmd.append("--color=always" if sys.stdout.isatty() else "--color=never")
  if word_regexp:
    cmd.append("-w")
  cmd.extend(grep_args)
  for pattern in patterns:
    cmd.extend(("-e", pattern))
  cmd.extend(scope_arguments)
  return tuple(cmd)


def run_command(cmd):
  return subprocess.run(cmd, stdout=subprocess.PIPE)


def _output_error(offset, message):
  return ValueError(
    f"unexpected git grep output at byte {offset}: {message}")


def _find_delimiter(data, delimiter, offset, description):
  position = data.find(delimiter, offset)
  if position < 0:
    raise _output_error(offset, f"missing {description}")
  return position


def _parse_number(value, offset, description, *, allow_zero=False):
  if not value.isdigit():
    raise _output_error(offset, f"invalid {description}")
  number = int(value)
  if number == 0 and not allow_zero:
    raise _output_error(offset, f"invalid {description}")
  return number


def _context_separator_end(data, offset):
  line_end = data.find(b"\n", offset)
  if line_end < 0:
    return None
  candidate = re.sub(
    rb"\x1b\[[0-9;]*m", b"", data[offset:line_end])
  if candidate == b"--":
    return line_end + 1
  return None


def _parse_line_record(data, offset, *, context):
  if context and (separator_end := _context_separator_end(data, offset)):
    return None, separator_end

  path_end = _find_delimiter(data, b"\0", offset, "path delimiter")
  if path_end == offset:
    raise _output_error(offset, "empty path")
  line_start = path_end + 1
  line_end = _find_delimiter(
    data, b"\0", line_start, "line-number delimiter")
  line_number = _parse_number(
    data[line_start:line_end], line_start, "line number")

  text_end = _find_delimiter(
    data, b"\n", line_end + 1, "record terminator")
  column_end = data.find(b"\0", line_end + 1, text_end)
  if column_end < 0:
    if not context:
      raise _output_error(line_end + 1, "missing column")
    column = None
    text_start = line_end + 1
  else:
    column_start = line_end + 1
    column = _parse_number(
      data[column_start:column_end], column_start, "column")
    text_start = column_end + 1

  return SearchResult(
    path=os.fsdecode(data[offset:path_end]),
    line=line_number,
    column=column,
    text=data[text_start:text_end].decode("utf-8", "replace"),
  ), text_end + 1


def _parse_match_results(data):
  offset = 0
  while offset < len(data):
    result, offset = _parse_line_record(data, offset, context=False)
    yield result


def _make_context_block(lines):
  try:
    return ContextBlock(lines[0].path, tuple(lines))
  except (IndexError, ValueError) as error:
    raise ValueError(f"unexpected git grep context output: {error}") from error


def _parse_context_blocks(data):
  offset = 0
  lines = []
  separator_offset = None
  while offset < len(data):
    record_offset = offset
    result, offset = _parse_line_record(data, offset, context=True)
    if result is None:
      if not lines:
        raise _output_error(record_offset, "empty context block")
      yield _make_context_block(lines)
      lines = []
      separator_offset = record_offset
    else:
      if lines and result.path != lines[0].path:
        yield _make_context_block(lines)
        lines = []
      lines.append(result)
      separator_offset = None

  if lines:
    yield _make_context_block(lines)
  elif separator_offset is not None:
    raise _output_error(separator_offset, "empty context block")


def _parse_path_results(data):
  offset = 0
  while offset < len(data):
    path_end = _find_delimiter(data, b"\0", offset, "path delimiter")
    if path_end == offset:
      raise _output_error(offset, "empty path")
    yield PathResult(os.fsdecode(data[offset:path_end]))
    offset = path_end + 1


def _parse_count_results(data):
  offset = 0
  while offset < len(data):
    path_end = _find_delimiter(data, b"\0", offset, "path delimiter")
    if path_end == offset:
      raise _output_error(offset, "empty path")
    count_start = path_end + 1
    count_end = _find_delimiter(
      data, b"\n", count_start, "count terminator")
    count = _parse_number(
      data[count_start:count_end], count_start, "count", allow_zero=True)
    yield CountResult(os.fsdecode(data[offset:path_end]), count)
    offset = count_end + 1


def parse_structured_records(data, mode):
  if mode is StructuredOutputMode.MATCHES:
    yield from _parse_match_results(data)
  elif mode is StructuredOutputMode.CONTEXT:
    yield from _parse_context_blocks(data)
  elif mode is StructuredOutputMode.PATHS:
    yield from _parse_path_results(data)
  elif mode is StructuredOutputMode.COUNTS:
    yield from _parse_count_results(data)
  else:
    raise ValueError(f"unknown structured output mode: {mode!r}")


def parse_null_results(data):
  yield from parse_structured_records(data, StructuredOutputMode.MATCHES)


def parse_null_paths(data):
  for result in parse_structured_records(data, StructuredOutputMode.PATHS):
    yield SearchResult(result.path, None, None, "")


def select_result(results, index, *, clamp):
  if not results:
    return None
  if clamp:
    normalized_index = min(max(index, 1), len(results))
    return results[normalized_index - 1]
  if index < 1 or index > len(results):
    raise IndexError(index)
  return results[index - 1]


def write_json_results(results):
  for result in results:
    print(json.dumps({
      "path": result.path,
      "line": result.line,
      "column": result.column,
      "text": result.text,
    }))


def write_result(result):
  print(f"{result.path}:{result.line}:{result.text}")


def write_context(result, context=12):
  if result.line is None:
    try:
      sys.stdout.buffer.write(Path(result.path).read_bytes())
    except OSError as error:
      sys.stderr.write(f"Error reading {result.path}: {error}\n")
      return 2
    return 0

  try:
    lines = Path(result.path).read_text(errors="replace").splitlines()
  except OSError as error:
    sys.stderr.write(f"Error reading {result.path}: {error}\n")
    return 2

  first = max(1, result.line - context)
  last = min(len(lines), result.line + context)
  print(f"{result.path}:{result.line}:{result.column}")
  for line_number in range(first, last + 1):
    marker = ">" if line_number == result.line else " "
    print(f"{marker}{line_number:6} | {lines[line_number - 1]}")
  return 0


def open_search_result(tool, result):
  if not tool:
    return write_context(result)

  command = shlex.split(tool)
  if not command:
    return write_context(result)
  executable = Path(command[0]).name
  if result.line is None:
    command.append(result.path)
  elif executable in ("vim", "nvim"):
    command.extend((result.path, f"+{result.line}"))
  else:
    command.append(f"{result.path}:{result.line}")
  return subprocess.run(command).returncode
