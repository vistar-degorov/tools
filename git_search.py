from dataclasses import dataclass
from itertools import product
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
  line: int
  column: int
  text: str


class Config:
  def __init__(self, flags, test_indicators=("[Tt]est",), test_mode="exclude"):
    self.debug_mode = False
    self.hide_filenames = False
    self.only_filenames = False
    self.pats = ("{PREFIX}*{EXTEN}",)
    self.mods = ()
    self.set_test_mode(test_mode, test_indicators)
    self.flags = tuple(self._filter_flags(flags, test_indicators))
    if not self.hide_filenames and not self.only_filenames:
      self.flags += ("-n",)

  def set_test_mode(self, mode, test_indicators):
    if mode == "include":
      self.mods = ()
      self.pats = ("{PREFIX}*{EXTEN}",)
    elif mode == "only":
      self.mods = ()
      self.pats = tuple(
        "{{PREFIX}}*{word}*{{EXTEN}}".format(word=word)
        for word in test_indicators
      )
    else:
      self.mods = tuple(
        ":!:*{word}*".format(word=word)
        for word in test_indicators
      )
      self.pats = ("{PREFIX}*{EXTEN}",)

  def _filter_flags(self, flags, test_indicators):
    for flag in flags:
      if flag in ("+debug", "+print"):
        self.debug_mode = True
      elif flag == "+t":
        self.set_test_mode("include", test_indicators)
      elif flag == "+to":
        self.set_test_mode("only", test_indicators)
      else:
        if flag.startswith("-") and not flag.startswith("--"):
          self.hide_filenames = self.hide_filenames or "h" in flag
          self.only_filenames = self.only_filenames or "l" in flag
        yield flag

  def format(self, prefix, extensions):
    pats = (
      pat.format(PREFIX=prefix, EXTEN=ext)
      for pat, ext in product(self.pats, extensions)
    )
    return (*self.mods, *pats)


def split_result_idx(query):
  if match := re.search(r"/(\d+)$", query):
    return query[:match.start()], int(match.group(1))
  return query, None


def remove_color_codes(text):
  return re.sub(r"\x1b\[[0-9;]*m", "", text)


def resolve_setting(name, fallback):
  if value := os.getenv(name):
    return value
  if value := os.getenv(f"{name}_DEFAULT"):
    return value
  return fallback


def resolve_extensions(values):
  if values:
    extensions = [item for value in values for item in value.split()]
  else:
    extensions = resolve_setting("GG_SEARCH_EXT", ".py").split()
  return tuple(
    extension if extension.startswith(".") else f".{extension}"
    for extension in extensions
  )


def build_command(
  patterns,
  grep_args,
  pathspecs,
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
  cmd.extend(("--", *pathspecs))
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
  if executable in ("vim", "nvim"):
    command.extend((result.path, f"+{result.line}"))
  else:
    command.append(f"{result.path}:{result.line}")
  return subprocess.run(command).returncode


def has_output_modifiers(arguments):
  long_options = {
    "--after-context", "--before-context", "--break", "--context",
    "--color",
    "--count", "--files-with-matches", "--files-without-match",
    "--function-context", "--heading", "--name-only", "--no-filename",
    "--null", "--show-function",
  }
  for argument in arguments:
    if argument in long_options:
      return True
    if any(argument.startswith(f"{option}=") for option in long_options):
      return True
    if re.match(r"-[ABC]\d*$", argument):
      return True
    if re.match(r"-[cLlhpW]+$", argument):
      return True
  return False


# Compatibility helpers used by open_result.py.
def iter_search_results(lines, predicate=None):
  for line in lines:
    line = remove_color_codes(line.decode("utf-8"))
    if predicate and not predicate(line):
      continue
    match line.split(":"):
      case (file_path, line_num, _, *_):
        if line_num.isdecimal():
          yield file_path, line_num
        else:
          yield file_path
      case (file_path, _) | (file_path,):
        yield file_path


def _open_file(tool, file_path, line_num=None, ctx_window=12):
  if line_num is None:
    cmd = ("cat" if tool == "" else tool), file_path
  elif tool == "":
    line_num = int(line_num)
    head, tail = max(1, line_num - ctx_window), line_num + ctx_window
    cmd = "sed", "-n", f"{head},{tail}p", file_path
  elif tool in ("vim", "nvim"):
    cmd = tool, file_path, f"+{line_num}"
  else:
    cmd = tool, f"{file_path}:{line_num}"
  subprocess.run(cmd)


def open_with(tool, args):
  match args:
    case None:
      pass
    case (file_path, line_num):
      _open_file(tool, file_path, line_num)
    case file_path:
      _open_file(tool, file_path)


def get_search_result(lines, result_idx, config, predicate=None):
  if config.hide_filenames:
    return
  if config.only_filenames:
    filepaths = lines
  else:
    filepaths = iter_search_results(lines, predicate)

  filepath_to_open = None
  for filepath_to_open in filepaths:
    if result_idx < 2:
      break
    result_idx -= 1
  return filepath_to_open
