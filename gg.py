#! /usr/bin/python

import os
import re
import subprocess as sp
import shlex
import sys
from pathlib import PurePath as Path
from finddef import Config, split_result_idx, get_search_result, open_with, remove_color_codes

if __name__ == "__main__":
  _PREFIX = os.getenv("GG_SEARCH_IN") or os.getenv("GG_SEARCH_IN_DEFAULT") or "./"
  _EXTEN = os.getenv("GG_SEARCH_EXT") or os.getenv("GG_SEARCH_EXT_DEFAULT") or ".py"
  _EXTEN = _EXTEN.split()

  _TOOL = os.getenv("GG_TOOL") or os.getenv("GG_TOOL_DEFAULT") or ""

  _IMPORT_RX = []
  if {".py", ".ts", ".tsx"}.intersection(_EXTEN):
    _IMPORT_RX.append(re.compile(r"\bimport "))
  if ".coffee" in _EXTEN:
    _IMPORT_RX.append(re.compile(r"} *= *require\b"))

  def has_no_import(text):
    return all(rx.search(text) is None for rx in _IMPORT_RX)

  _TEST_INDICATORS = ["[Tt]est"]
  if {".coffee", ".ts", ".tsx"}.intersection(_EXTEN):
    _TEST_INDICATORS.append("[Ss]pec")

  main_arg = Path(sys.argv[0]).name
  args = sys.argv[1:]
  flags, name = args[:-1], args[-1]
  name, result_idx = split_result_idx(name)
  if name == "":
    sys.exit("name cannot be empty")

  include_imports = False

  if main_arg == "gq": # search for single-quoted names
    name = "'" + name + "'"

  elif main_arg == "gqq": # search for double-quoted names
    name = '"' + name + '"'

  else:
    # if True, do not filter out imports
    include_imports = main_arg == "gi" or not has_no_import(name)

    if re.match(r"\w", name):
      name = r"\b" + name

    if re.match(r"\w", name[-1:]):
      name = name + r"\b"

  config = Config(flags, test_indicators=_TEST_INDICATORS)
  color_flag = ("--color=always",) if sys.stdout.isatty() else ()
  cmd = (
    "git", "grep", *color_flag, *config.flags,
    name, '--', *config.format(_PREFIX, _EXTEN),
  )
  if config.debug_mode:
    print(shlex.join(cmd))
    sys.exit(0)

  try:
    outp = sp.run(cmd, stdout=sp.PIPE).stdout
  except sp.CalledProcessError:
    sys.exit(0)

  if include_imports:
    if result_idx is None:
      sys.stdout.buffer.write(outp)
    else:
      open_with(_TOOL, get_search_result(outp.splitlines(), result_idx, config))
  elif result_idx is None:
    for line in outp.splitlines():
      if has_no_import(remove_color_codes(line.decode("utf-8"))):
        sys.stdout.buffer.write(line + b"\n")
  else:
    open_with(
      _TOOL,
      get_search_result(outp.splitlines(), result_idx, config, predicate=has_no_import)
    )
