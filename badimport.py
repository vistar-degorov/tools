#! /usr/bin/python

import ast
from pathlib import Path
import sys
import tokenize


VERSION = "0.0.2"


class Token(tokenize.TokenInfo):
  def __eq__(self, value: str) -> bool:
    return self.string == value


def _tokenize_stream(input_stream):
  def unindented_readline():
    return input_stream.readline().lstrip(" \t\f")

  try:
    tokens = (Token(*t) for t in tokenize.generate_tokens(unindented_readline))
    yield from tokens
  except (IndentationError, tokenize.TokenError):
    # Analyze all tokens produced before an unrecoverable tokenization error.
    pass


def _iter_statements(tokens):
  current = []
  bracket_depth = 0
  ignored = {
    tokenize.COMMENT,
    tokenize.ENCODING,
    tokenize.INDENT,
    tokenize.DEDENT,
  }

  for token in tokens:
    if token.type in ignored:
      continue
    if token.type == tokenize.ENDMARKER:
      break
    if token.type in (tokenize.NEWLINE, tokenize.NL) or (
      token.type == tokenize.ERRORTOKEN and token in ("\n", "\r\n")):
      if bracket_depth == 0 and current:
        yield current
        current = []
      continue

    # Recover after an unmatched opening bracket when a later physical line
    # clearly starts another import statement.
    starts_import = (
      token.type == tokenize.NAME
      and token in ("from", "import")
      and token.line[:token.start[1]].strip() == ""
    )
    continues_from_import = (
      current
      and current[0] == "from"
      and token == "import"
      and "import" not in current[1:]
    )
    if current and starts_import and not continues_from_import:
      yield current
      current = []
      bracket_depth = 0

    if token.type == tokenize.OP:
      if token in ("(", "[", "{"):
        bracket_depth += 1
      elif token in (")", "]", "}"):
        bracket_depth = max(0, bracket_depth - 1)
      elif bracket_depth == 0 and token in (";", ":"):
        if current:
          yield current
          current = []
        continue

    current.append(token)

  if current:
    yield current


def _is_name(token):
  return token.type == tokenize.NAME


def _names_in_fstring(token):
  quote_index = min(
    (
      index
      for index, character in enumerate(token.string)
      if character in "'\""
    ),
    default=len(token.string),
  )
  if "f" not in token.string[:quote_index].lower():
    return

  try:
    expression = ast.parse(token.string, mode="eval").body
  except SyntaxError:
    return
  if not isinstance(expression, ast.JoinedStr):
    return

  for node in ast.walk(expression):
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
      yield node.id


def _usage_names(token):
  if _is_name(token):
    yield token.string
  elif token.type == tokenize.STRING:
    yield from _names_in_fstring(token)


def _parse_dotted_name(statement, index):
  if index >= len(statement) or not _is_name(statement[index]):
    return None, index
  if statement[index] in ("as", "import"):
    return None, index

  parts = [statement[index].string]
  index += 1
  while (
    index + 1 < len(statement)
    and statement[index] == "."
    and _is_name(statement[index + 1])
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
    elif _is_name(statement[index]):
      original_name = statement[index].string
      next_index = index + 1

    if original_name is None:
      break
    index = next_index

    if index + 1 < len(statement) and statement[index] == "as" and _is_name(statement[index + 1]):
      alias = statement[index + 1].string
      index += 2

    if alias:
      bound_name = alias
    elif allow_dotted:
      bound_name = original_name.split(".", 1)[0]
    else:
      bound_name = original_name
    imported_names.append((alias or original_name, bound_name))

    if not (index < len(statement) and statement[index] == ","):
      break
    index += 1

  return imported_names, index


def _parse_from_import(statement, import_index):
  index = import_index + 1
  parenthesized = False

  if (index < len(statement) and statement[index] == "("):
    parenthesized = True
    index += 1

  if (index < len(statement) and statement[index] == "*"):
    return [("*", "*")], index + 1

  imported_names, index = _parse_import_names(statement, index, allow_dotted=False)
  if (parenthesized and index < len(statement) and statement[index] == ")"):
    index += 1

  return imported_names, index


def _parse_import_statement(statement):
  match statement:
    case "import", *_:
      imported_names, end_index = _parse_import_names(
        statement, 1, allow_dotted=True)

    case "from", *tail:
      try:
        import_index = tail.index("import") + 1
      except ValueError:
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


def parse_imports(input_stream):
  imports = []
  usages = {}

  tokens = _tokenize_stream(input_stream)
  for statement in _iter_statements(tokens):
    usage_index = 0
    if (parsed := _parse_import_statement(statement)) is not None:
      statement_imports, usage_index = parsed
      imports.extend(statement_imports)

    for token in statement[usage_index:]:
      for name in _usage_names(token):
        usages.setdefault(name, []).append(token.start)

  return imports, usages


def main():
  if sys.argv[1:] in (["--version"], ["-v"]):
    print(VERSION)
    sys.exit(0)

  for filename in sys.argv[1:]:
    if filename.startswith("-"):
      sys.stderr.write(f"Ignoring argument {filename}\n")
      continue

    if Path(filename).name == "__init__.py":
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

      used_later = any(
        position > end_position
        for position in usages.get(bound_name, ())
      )

      if not used_later or duplicate:
        suffix = "  (duplicate)" if duplicate else ""
        print(f"{list_header}    {displayed_name}{suffix}")
        list_header = ""


if __name__ == "__main__":
  main()
