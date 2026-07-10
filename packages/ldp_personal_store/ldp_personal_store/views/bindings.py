"""Portable, injection-safe parameter binding for view CONSTRUCT templates.

Standard SPARQL 1.1 Protocol carries no ``initBindings`` over the wire, so a view's
declared parameters are bound by rewriting the query itself: a ``VALUES`` block that
constrains the parameter variables is injected as the first element of the CONSTRUCT's
WHERE group, where it binds those variables for the whole group. Each supplied value is
serialized through its rdflib term's own ``.n3()`` — the RDF layer escapes quotes,
braces, and IRIs — so a value can never break out of its literal and alter the query.
This is the portable equivalent of the non-standard ``binding-<name>`` extension: an
arbitrary SPARQL server binds it, ours included, with no server-side cooperation.
"""

from pyparsing.exceptions import ParseException
from rdflib.plugins.sparql.parser import parseQuery

from ldp_common.viewmodel import ParamDecl, ParamTypeName, param_term


class BindingError(ValueError):
    """A view template could not be parameterized with the supplied bindings."""


def _scan_where_group(query: str) -> tuple[int | None, int | None]:
    """Locate, ignoring strings/comments/IRIs, the first ``{`` and first ``WHERE`` keyword.

    Returns ``(first_brace_index, where_keyword_index)``; either may be ``None`` when the
    query contains no such token outside a literal.
    """
    first_brace: int | None = None
    where_at: int | None = None
    i = 0
    n = len(query)
    while i < n:
        ch = query[i]
        if ch == "#":  # comment to end of line
            nl = query.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if ch in "'\"":  # string literal (handles the ''' / \"\"\" long forms)
            quote = query[i : i + 3] if query[i : i + 3] in ("'''", '"""') else ch
            i += len(quote)
            while i < n:
                if query[i] == "\\":
                    i += 2
                    continue
                if query.startswith(quote, i):
                    i += len(quote)
                    break
                i += 1
            continue
        if ch == "<":  # IRI reference (cannot contain '{' or whitespace)
            close = query.find(">", i)
            i = n if close == -1 else close + 1
            continue
        if ch == "{":
            if first_brace is None:
                first_brace = i
            i += 1
            continue
        if where_at is None and (ch in "wW") and query[i : i + 5].upper() == "WHERE":
            before = query[i - 1] if i > 0 else " "
            after = query[i + 5] if i + 5 < n else " "
            if not before.isalnum() and before != "_" and not after.isalnum() and after != "_":
                where_at = i
        i += 1
    return first_brace, where_at


def _group_bounds(query: str, open_brace: int) -> int:
    """Return the index of the ``}`` matching the group opened at *open_brace*."""
    depth = 0
    i = open_brace
    n = len(query)
    while i < n:
        ch = query[i]
        if ch == "#":
            nl = query.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if ch in "'\"":
            quote = query[i : i + 3] if query[i : i + 3] in ("'''", '"""') else ch
            i += len(quote)
            while i < n:
                if query[i] == "\\":
                    i += 2
                    continue
                if query.startswith(quote, i):
                    i += len(quote)
                    break
                i += 1
            continue
        if ch == "<":
            close = query.find(">", i)
            i = n if close == -1 else close + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise BindingError("unbalanced braces in view template")


def _serialize_term(value: str, param_type: ParamTypeName) -> str:
    # rdflib refuses to serialize a term whose lexical form is not a valid RDF term
    # (e.g. an IRI carrying a space) with a bare Exception; contain it as a BindingError
    # so a hostile or malformed value is a controlled failure, never an escaped 500.
    try:
        return param_term(value, param_type).n3()
    except BindingError:
        raise
    except Exception as exc:  # noqa: BLE001  rdflib raises bare Exception on bad terms
        raise BindingError(f"value {value!r} is not a serializable {param_type} term") from exc


def _values_block(bound: dict[str, str], decls: list[ParamDecl]) -> str:
    ordered = [decl for decl in decls if decl.name in bound]
    variables = " ".join(f"?{decl.name}" for decl in ordered)
    terms = " ".join(_serialize_term(bound[decl.name], decl.type) for decl in ordered)
    return f"VALUES ({variables}) {{ ({terms}) }}"


def inject_values(template: str, bound: dict[str, str], decls: list[ParamDecl]) -> str:
    """Return *template* rewritten to bind *bound* via an injected ``VALUES`` block.

    With no supplied bindings the template is returned unchanged — every parameter
    variable stays free, so the view is not narrowed on any axis. Both the explicit
    ``CONSTRUCT { … } WHERE { … }`` form and the ``CONSTRUCT WHERE { … }`` shorthand are
    supported. Raises :class:`BindingError` if the rewritten query does not parse.
    """
    if not bound:
        return template

    block = _values_block(bound, decls)
    first_brace, where_at = _scan_where_group(template)
    if where_at is None:
        raise BindingError("view template has no WHERE clause to parameterize")

    if first_brace is None or where_at < first_brace:
        # CONSTRUCT WHERE { T } shorthand: the WHERE group is also the construct template.
        # Expand to the equivalent explicit form so the VALUES block has a home that is a
        # group graph pattern (the shorthand's body may only be a triples template).
        group_open = template.index("{", where_at)
        group_close = _group_bounds(template, group_open)
        head = template[:where_at]
        body = template[group_open + 1 : group_close]
        tail = template[group_close + 1 :]
        rewritten = f"{head}{{{body}}} WHERE {{ {block} {body}}}{tail}"
    else:
        group_open = template.index("{", where_at)
        rewritten = f"{template[: group_open + 1]} {block} {template[group_open + 1 :]}"

    try:
        parseQuery(rewritten)
    except ParseException as exc:
        raise BindingError(f"parameterized view template did not parse: {exc}") from exc
    return rewritten
