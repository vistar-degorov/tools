#! /usr/bin/python

import argparse
from pathlib import Path
import sys

from parser import PythonImportParser

VERSION = "0.0.3"

def parse_imports(input_stream):
  return PythonImportParser(input_stream).finish()


def _parse_arguments(arguments=None):
  parser = argparse.ArgumentParser(
    description="Find unused and duplicate imports in Python code.")
  parser.add_argument(
    "-v", "--version", action="version", version=VERSION)
  parser.add_argument(
    "-i", "--include-init", action="store_true",
    help="include __init__.py files")
  parser.add_argument("filenames", metavar="FILE", nargs="*")
  return parser.parse_intermixed_args(arguments)


def main():
  arguments = _parse_arguments()

  for filename in arguments.filenames:
    if not arguments.include_init and Path(filename).name == "__init__.py":
      continue

    try:
      with open(filename, 'r') as istr:
        imports, usages = parse_imports(istr)

    except FileNotFoundError:
      sys.stderr.write(f"Error reading {filename}\n")
      continue

    imported_names_in_file = set()
    list_header = f"- {filename}:\n"
    for displayed_name, bound_name, end_position in imports:
      if displayed_name == "*":
        continue

      duplicate = displayed_name in imported_names_in_file
      imported_names_in_file.add(displayed_name)

      used_later = usages.get(bound_name, (0, 0)) > end_position
      if not used_later or duplicate:
        suffix = "  (duplicate)" if duplicate else ""
        print(f"{list_header}    {displayed_name}{suffix}")
        list_header = ""


if __name__ == "__main__":
  main()
