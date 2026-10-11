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


def _parse_number(value, offset, description):
  if value.isdigit():
    return int(value)
  raise _output_error(offset, f"invalid {description}")


def _parse_line_records(data, with_context):
  record_rx = re.compile(br"([^\0]+)\0([^\0]+)\0([^\n]*)\n")
  ctx_separator_rx = re.compile(br"(\x1b\[[0-9;]*m)?--(\x1b\[[0-9;]*m)?\n")
  offset = 0

  while True:
    if with_context and (separator := ctx_separator_rx.match(data, offset)):
      yield None, offset
      offset = separator.end()
      continue

    if (record := record_rx.match(data, offset)) is None:
      if offset < len(data):
        raise _output_error(offset, "invalid line record")
      break

    path, line_number, rest = record.group(1, 2, 3)
    line_number = _parse_number(line_number, record.start(2), "line number")

    column_end = rest.find(b"\0")
    if column_end < 0:
      if not with_context:
        raise _output_error(record.start(3), "missing column")
      column = None
    else:
      column = _parse_number(rest[:column_end], record.start(3), "column")
      rest = rest[column_end + 1:]

    yield SearchResult(
      path=os.fsdecode(path),
      line=line_number,
      column=column,
      text=rest.decode("utf-8", "replace"),
    ), offset
    offset = record.end()


def _make_context_block(lines):
  if not any(line.is_match for line in lines):
    raise ValueError("context contains no matches")
  return ContextBlock(lines[0].path, tuple(lines))


def _parse_context_blocks(data):
  lines = []
  separator_offset = None

  for result, offset in _parse_line_records(data, with_context=True):
    if result is None:
      if not lines:
        raise _output_error(offset, "empty context block")
      yield _make_context_block(lines)
      lines = []
      separator_offset = offset
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
  record_rx = re.compile(br"([^\0]+)\0")
  offset = 0
  while (record := record_rx.match(data, offset)) is not None:
    yield PathResult(os.fsdecode(record.group(1)))
    offset = record.end()
  if offset < len(data):
    raise _output_error(offset, "invalid path record")


def _parse_count_results(data):
  record_rx = re.compile(br"([^\0]+)\0([^\n]*)\n")
  offset = 0
  while (record := record_rx.match(data, offset)) is not None:
    path, count = record.group(1, 2)
    count = _parse_number(count, record.start(2), "count")
    yield CountResult(os.fsdecode(path), count)
    offset = record.end()
  if offset < len(data):
    raise _output_error(offset, "invalid count record")


def parse_structured_records(data, mode):
  if mode is StructuredOutputMode.MATCHES:
    for result, _ in _parse_line_records(data, with_context=False):
      yield result
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
