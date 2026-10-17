#! /usr/bin/python

import os
import re
import sys

from git_search_args import resolve_setting
from git_search import (
  SearchResult,
  open_search_result,
  remove_color_codes,
  select_result,
)


def _parse_result(line):
  line = remove_color_codes(line)
  if match := re.match(br"^(.*?):(\d+):(.*)$", line):
    path, line_number, text = match.groups()
    return SearchResult(os.fsdecode(path), int(line_number), 0, text)
  return SearchResult(os.fsdecode(line), None, None, b"")


if __name__ == "__main__":
  if sys.stdin.isatty():
    sys.exit(0)

  results = [
    _parse_result(line)
    for line in sys.stdin.buffer.read().splitlines()
  ]

  idx = 0
  if len(sys.argv) > 1:
    idx = int(sys.argv[1])

  result = select_result(results, idx, clamp=True)
  if result is not None:
    tool = resolve_setting("GG_TOOL", "")
    sys.exit(open_search_result(tool, result))
