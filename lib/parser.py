import ast
from itertools import chain
import tokenize


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


def _offset_position(position, line_offset):
  if not line_offset:
    return position
  line, column = position
  return line + line_offset, column


def _finish_import_candidate(statement, imports, bound_names, usages, line_offset=0):
  if (import_stmt := _parse_import_statement(statement)) is None:
    usage_index = 0
  else:
    statement_imports, usage_index = import_stmt
    imports.extend(
      (displayed_name, bound_name, _offset_position(end_position, line_offset))
      for displayed_name, bound_name, end_position in statement_imports
    )
    bound_names.update(
      bound_name for _, bound_name, _ in statement_imports)

  # A malformed import can have tokens after the part that was parsed.
  for token in statement[usage_index:]:
    if token.string in bound_names:
      usages[token.string] = _offset_position(token.start, line_offset)
    elif bound_names and token.type == tokenize.STRING:
      for name in _names_in_fstring(token):
        if name in bound_names:
          usages[name] = _offset_position(token.start, line_offset)


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
    def _unindented_readline():
      return input_stream.readline().lstrip(" \t\f")
    self._tokens = tokenize.generate_tokens(_unindented_readline)

    self.imports = []
    self.usages = {}
    self._bound_names = set()
    self._import_candidate = None
    self._statement_start = True
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

  def _advance(self, through_line=None):
    if self._done:
      return

    line_offset = self._line_offset
    local_through_line = None if through_line is None else through_line - line_offset
    tokens = self._tokens
    if self._pending_token is not None:
      tokens = chain((self._pending_token,), tokens)
      self._pending_token = None

    # binding these to local variables makes it go faster
    ignored_token_types = self._ignored_token_types
    imports = self.imports
    usages = self.usages
    bound_names = self._bound_names
    import_candidate = self._import_candidate
    statement_start = self._statement_start
    token = None

    try:
      for token in tokens:
        if local_through_line is not None and token.start[0] > local_through_line:
          self._pending_token = token
          self._advanced_through = through_line
          break

        t_type = token.type

        if t_type in ignored_token_types:
          continue
        if t_type == tokenize.ENDMARKER:
          self._advanced_through = token.end[0] + line_offset
          self._done = True
          break

        t_value = token.string
        if t_type == tokenize.NL:
          continue
        if t_type == tokenize.NEWLINE or (
          t_type == tokenize.ERRORTOKEN and t_value in ("\n", "\r\n")):
          if import_candidate is not None:
            _finish_import_candidate(
              import_candidate, imports, bound_names, usages, line_offset)
            import_candidate = None
          statement_start = True
          continue

        # Recover when a later physical line clearly starts another import after
        # an unmatched opening bracket or explicit line continuation.
        if (
          t_value in ("from", "import")
          and not statement_start
          and token.start[1] == 0
        ):
          continues_from_import = (
            t_value == "import"
            and import_candidate is not None
            and import_candidate[0].string == "from"
            and all(part.string != "import" for part in import_candidate)
          )
          if not continues_from_import:
            if import_candidate is not None:
              _finish_import_candidate(
                import_candidate, imports, bound_names, usages, line_offset)
              import_candidate = None
            statement_start = True

        if t_value in (";", ":"):
          if import_candidate is not None:
            _finish_import_candidate(
              import_candidate, imports, bound_names, usages, line_offset)
            import_candidate = None
          statement_start = True
          continue

        if import_candidate is not None:
          import_candidate.append(token)
        elif statement_start and t_value in ("from", "import"):
          import_candidate = [token]
        else:
          if t_value in bound_names:
            usages[t_value] = _offset_position(token.start, line_offset)
          elif bound_names and t_type == tokenize.STRING:
            for name in _names_in_fstring(token):
              if name in bound_names:
                usages[name] = _offset_position(token.start, line_offset)

        statement_start = False
      else:
        if token is not None:
          self._advanced_through = token.end[0] + line_offset
        self._done = True

    except (IndentationError, tokenize.TokenError):
      # Analyze all tokens produced before an unrecoverable tokenization error.
      if token is not None:
        self._advanced_through = token.end[0] + line_offset
      self._done = True

    if self._done and import_candidate is not None:
      _finish_import_candidate(
        import_candidate, imports, bound_names, usages, line_offset)
      import_candidate = None

    self._import_candidate = import_candidate
    self._statement_start = statement_start

  def advance_through(self, line):
    """Process tokens through an absolute source line without finishing."""

    if line <= self._advanced_through or self._done:
      return self

    self._advance(line)
    return self

  def finish(self):
    """Consume the remaining input and return imports and usages."""

    self._advance()
    return self.imports, self.usages
