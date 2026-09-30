from dataclasses import dataclass
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


def parse_null_results(output):
  offset = 0
  output_length = len(output)
  while offset < output_length:
    path_end = output.find(b"\0", offset)
    line_end = output.find(b"\0", path_end + 1)
    column_end = output.find(b"\0", line_end + 1)
    text_end = output.find(b"\n", column_end + 1)
    if min(path_end, line_end, column_end, text_end) < 0:
      raise ValueError("unexpected git grep output")

    yield SearchResult(
      path=os.fsdecode(output[offset:path_end]),
      line=int(output[path_end + 1:line_end]),
      column=int(output[line_end + 1:column_end]),
      text=output[column_end + 1:text_end].decode("utf-8", "replace"),
    )
    offset = text_end + 1


def parse_null_paths(output):
  for path in output.split(b"\0"):
    if path:
      yield SearchResult(os.fsdecode(path), None, None, "")


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
