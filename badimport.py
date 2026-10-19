#! /usr/bin/python

import argparse
import ast
from pathlib import Path
import sys
import tokenize


VERSION = "0.0.2"


def _tokenize_stream(input_stream):
  def unindented_readline():
    return input_stream.readline().lstrip(" \t\f")

  try:
    yield from tokenize.generate_tokens(unindented_readline)
  except (IndentationError, tokenize.TokenError):
    # Analyze all tokens produced before an unrecoverable tokenization error.
    pass


def _names_in_fstring(token):
  literal = token.string
  prefix = literal[:2].lower()
  if not (prefix.startswith("f") or prefix == "rf"):
    return

  try:
    expression = ast.parse(literal, mode="eval").body
  except SyntaxError:
    return
  if not isinstance(expression, ast.JoinedStr):
    return

  for node in ast.walk(expression):
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
      yield node.id


def _parse_dotted_name(statement, index):
  if index >= len(statement) or statement[index].type != tokenize.NAME:
    return None, index
  if statement[index].string in ("as", "import"):
    return None, index

  parts = [statement[index].string]
  index += 1
  while (
    index + 1 < len(statement)
    and statement[index].string == "."
    and statement[index + 1].type == tokenize.NAME
  ):
    parts.append(statement[index + 1].string)
    index += 2
  return ".".join(parts), index


def _parse_import_names(statement, index, *, allow_dotted):
  imported_names = []

  while index < len(statement):
    original_name, alias = None, None

    if allow_dotted:
      original_name, next_index = _parse_dotted_name(statement, index)
    elif statement[index].type == tokenize.NAME:
      original_name = statement[index].string
      next_index = index + 1

    if original_name is None:
      break
    index = next_index

    if index + 1 < len(statement) and statement[index].string == "as" \
      and statement[index + 1].type == tokenize.NAME:
      alias = statement[index + 1].string
      index += 2

    if alias:
      bound_name = alias
    elif allow_dotted:
      bound_name = original_name.split(".", 1)[0]
    else:
      bound_name = original_name
    imported_names.append((alias or original_name, bound_name))

    if not (index < len(statement) and statement[index].string == ","):
      break
    index += 1

  return imported_names, index


def _parse_from_import(statement, import_index):
  index = import_index + 1
  parenthesized = False

  if (index < len(statement) and statement[index].string == "("):
    parenthesized = True
    index += 1

  if (index < len(statement) and statement[index].string == "*"):
    return [("*", "*")], index + 1

  imported_names, index = _parse_import_names(statement, index, allow_dotted=False)
  if (parenthesized and index < len(statement)
    and statement[index].string == ")"):
    index += 1

  return imported_names, index


def _parse_import_statement(statement):
  if not statement:
    return None

  match statement[0].string:
    case "import":
      imported_names, end_index = _parse_import_names(
        statement, 1, allow_dotted=True)

    case "from":
      for import_index in range(1, len(statement)):
        if statement[import_index].string == "import":
          break
      else:
        return None

      module_name = "".join(token.string for token in statement[1:import_index])
      imported_names, end_index = _parse_from_import(statement, import_index)
      if module_name == "__future__":
        imported_names = []

    case _:
      return None

  end_index = max(1, end_index)
  end_position = statement[end_index - 1].end
  imports = [
    (displayed_name, bound_name, end_position)
    for displayed_name, bound_name in imported_names
  ]
  return imports, end_index


def _finish_import_candidate(statement, imports, bound_names, usages):
  if (import_stmt := _parse_import_statement(statement)) is None:
    usage_index = 0
  else:
    statement_imports, usage_index = import_stmt
    imports.extend(statement_imports)
    bound_names.update(
      bound_name for _, bound_name, _ in statement_imports)

  # A malformed import can have tokens after the part that was parsed.
  for token in statement[usage_index:]:
    if token.string in bound_names:
      usages[token.string] = token.start
    elif token.type == tokenize.STRING:
      for name in _names_in_fstring(token):
        if name in bound_names:
          usages[name] = token.start


class PythonImportParser:
  """Incrementally parse python imports while retaining state between advances.

  line_offset translates token positions when input_stream starts partway
  through a file. A stream beginning at source line N uses N - 1.
  """

  _ignored_token_types = frozenset((
    tokenize.COMMENT,
    tokenize.ENCODING,
    tokenize.INDENT,
    tokenize.DEDENT,
  ))

  def __init__(self, input_stream, *, line_offset=0):
    self.imports = []
    self.usages = {}
    self._bound_names = set()
    self._import_candidate = None
    self._statement_start = True
    self._tokens = iter(_tokenize_stream(input_stream))
    self._pending_token = None
    self._done = False
    self._line_offset = line_offset
    self._advanced_through = line_offset

  @property
  def done(self):
    return self._done

  @property
  def advanced_through(self):
    return self._advanced_through

  @property
  def import_in_progress(self):
    return self._import_candidate is not None

  def _offset_token(self, token):
    if not self._line_offset:
      return token
    start_line, start_column = token.start
    end_line, end_column = token.end
    return token._replace(
      start=(start_line + self._line_offset, start_column),
      end=(end_line + self._line_offset, end_column),
    )

  def _next_token(self):
    if self._pending_token is not None:
      token = self._pending_token
      self._pending_token = None
      return token

    try:
      return self._offset_token(next(self._tokens))
    except StopIteration:
      self._finish_input()
      return None

  def _finish_import_candidate(self):
    if self._import_candidate is None:
      return
    _finish_import_candidate(
      self._import_candidate, self.imports, self._bound_names, self.usages)
    self._import_candidate = None

  def _finish_input(self):
    if self._done:
      return
    self._finish_import_candidate()
    self._done = True

  def _process_token(self, token):
    t_type = token.type

    if t_type in self._ignored_token_types:
      return
    if t_type == tokenize.ENDMARKER:
      self._finish_input()
      return

    t_value = token.string
    if t_type == tokenize.NL:
      return
    if t_type == tokenize.NEWLINE or (
      t_type == tokenize.ERRORTOKEN and t_value in ("\n", "\r\n")):
      self._finish_import_candidate()
      self._statement_start = True
      return

    # Recover when a later physical line clearly starts another import after
    # an unmatched opening bracket or explicit line continuation.
    if (
      t_value in ("from", "import")
      and not self._statement_start
      and token.start[1] == 0
    ):
      continues_from_import = (
        t_value == "import"
        and self._import_candidate is not None
        and self._import_candidate[0].string == "from"
        and all(part.string != "import" for part in self._import_candidate)
      )
      if not continues_from_import:
        self._finish_import_candidate()
        self._statement_start = True

    if t_value in (";", ":"):
      self._finish_import_candidate()
      self._statement_start = True
      return

    if self._import_candidate is not None:
      self._import_candidate.append(token)
    elif self._statement_start and t_value in ("from", "import"):
      self._import_candidate = [token]
    else:
      if t_value in self._bound_names:
        self.usages[t_value] = token.start
      elif t_type == tokenize.STRING:
        for name in _names_in_fstring(token):
          if name in self._bound_names:
            self.usages[name] = token.start

    self._statement_start = False

  def advance_through(self, line):
    """Process tokens through an absolute source line without finishing."""

    if line <= self._advanced_through or self._done:
      return self

    while not self._done:
      token = self._next_token()
      if token is None:
        break
      if token.start[0] > line:
        self._pending_token = token
        self._advanced_through = max(self._advanced_through, line)
        break
      self._process_token(token)
      self._advanced_through = max(self._advanced_through, token.end[0])

    return self

  def finish(self):
    """Consume the remaining input and return imports and usages."""

    while not self._done:
      token = self._next_token()
      if token is None:
        break
      self._process_token(token)
      self._advanced_through = max(self._advanced_through, token.end[0])
    return self.imports, self.usages


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
