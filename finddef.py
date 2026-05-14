#! /usr/bin/python

from itertools import chain
from itertools import product
import os
import re
import subprocess as sp
import shlex
import sys

class Config:
  def __init__(self, flags, test_indicators=("[Tt]est",)):
    self.debug_mode = False # print grep command instead of executing it
    self.hide_filenames = False # -h flag
    self.only_filenames = False # -l flag

    self.pats = ("{PREFIX}*{EXTEN}",) # file patterns to search in

    # exclude tests from search
    self.mods = tuple(":!:*{word}*".format(word=word) for word in test_indicators)
    self.flags = tuple(self._filter_flags(flags, test_indicators))
    if not self.hide_filenames and not self.only_filenames:
      self.flags += ('-n',) # add line numbers to filenames

  def _filter_flags(self, flags, test_indicators):
    for flag in flags:
      if flag in ("+debug", "+print"):
        self.debug_mode = True

      elif flag == "+t": # include tests
        self.mods = ()

      elif flag == "+to": # tests only
        self.mods = ()
        self.pats = tuple("{{PREFIX}}*{word}*{{EXTEN}}".format(word=word)
          for word in test_indicators)

      elif flag.startswith('-'):
        if not flag.startswith('--'):
          self.hide_filenames = self.hide_filenames or 'h' in flag
          self.only_filenames = self.only_filenames or 'l' in flag
        yield flag

      else:
        sys.exit(f"unknown flag: {flag}")

  def format(self, prefix, extensions):
    pats = (pat.format(PREFIX=prefix, EXTEN=ext)
      for pat, ext in product(self.pats, extensions))
    return (*self.mods, *pats)

def split_result_idx(query):
  if rx := re.search(r'/(\d+)$', query):
    return query[:rx.start()], int(rx.group(1))
  else:
    return query, None

def remove_color_codes(text):
  return re.sub(r'\x1b\[[0-9;]*m', '', text)

def iter_search_results(lines, predicate=None):
  for line in lines:
    line = remove_color_codes(line.decode("utf-8"))
    if predicate and not predicate(line):
      continue

    match line.split(':'):
      case (file_path, line_num, _, *_):
        if line_num.isdecimal():
          yield file_path, line_num
        else:
          yield file_path

      case (file_path, _) | (file_path,):
        yield file_path

def _open_file(tool, file_path, line_num=None):
  if line_num is None:
    cmd = tool, file_path
  elif tool in ("vim", "nvim"):
    cmd = tool, file_path, f"+{line_num}"
  else:
    cmd = tool, f"{file_path}:{line_num}"
  sp.run(cmd)

def open_search_result(lines, result_idx, config, predicate=None, tool="subl"):
  if config.hide_filenames:
    return
  elif config.only_filenames:
    filepaths = lines
  else:
    filepaths = iter_search_results(lines, predicate)

  filepath_to_open = None
  for filepath_to_open in filepaths:
    if result_idx < 2:
      break
    result_idx -= 1

  match filepath_to_open:
    case None:
      pass
    case (file_path, line_num):
      _open_file(tool, file_path, line_num)
    case _:
      _open_file(tool, filepath_to_open)

if __name__ == "__main__":
  _PREFIX = os.getenv("GG_SEARCH_IN") or os.getenv("GG_SEARCH_IN_DEFAULT") or "./"
  _EXTEN = os.getenv("GG_SEARCH_EXT") or os.getenv("GG_SEARCH_EXT_DEFAULT") or ".py"
  _EXTEN = _EXTEN.split()

  pats = (
    r"\bclass \+{NAME} *:",
    r"\bclass \+{NAME} *(.\+) *:",
    r"\bdef \+{NAME} *(.*",
    r"\bdef \+{NAME} *(.*) *->.\+:",
  )
  assignment_pat = r"\b{NAME} \+= *[^=].*"

  args = sys.argv[1:]
  flags, name = args[:-1], args[-1]
  name, result_idx = split_result_idx(name)

  if name.endswith("="):
    pats = (assignment_pat,)
    name = name.rstrip('=')
  if name == "":
    sys.exit("name cannot be empty")

  pats = chain(*(("-e", pat.format(NAME=name)) for pat in pats))

  config = Config(flags)
  color_flag = ("--color=always",) if sys.stdout.isatty() else ()
  cmd = (
    "git", "grep", *color_flag, *config.flags,
    *pats, '--', *config.format(_PREFIX, _EXTEN),
  )
  if config.debug_mode:
    print(shlex.join(cmd))
    sys.exit(0)

  try:
    outp = sp.run(cmd, stdout=sp.PIPE).stdout
  except sp.CalledProcessError:
    sys.exit(0)

  if result_idx is None:
    sys.stdout.buffer.write(outp)
  else:
    open_search_result(outp.splitlines(), result_idx, config)
