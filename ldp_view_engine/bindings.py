"""Portable, injection-safe parameter binding for view CONSTRUCT templates."""

from pyparsing.exceptions import ParseException
from rdflib.plugins.sparql.parser import parseQuery

from ldp_common.viewmodel import ParamDecl, ParamTypeName, param_term


class BindingError(ValueError):
    """A view template could not be parameterized with the supplied bindings."""


def _scan_where_group(query: str) -> tuple[int | None, int | None, int | None]:
    first_brace: int | None = None
    where_at: int | None = None
    where_brace: int | None = None
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
            if where_at is not None and where_brace is None:
                where_brace = i
            i += 1
            continue
        if ch.isalpha() or ch in "_?$:":
            # Consume a whole name-ish token (keyword, ?/$ variable, or prefixed name) so
            # a bare WHERE is matched only when it stands alone, not when it is the tail of
            # ?where or ex:where — the ``\bWHERE\b`` regex failure this scan is built to avoid.
            start = i
            i += 1
            while i < n and (query[i].isalnum() or query[i] in "_-.:"):
                i += 1
            if where_at is None and query[start:i].upper() == "WHERE":
                where_at = start
            continue
        i += 1
    return first_brace, where_at, where_brace


def find_where_keyword(query: str) -> int | None:
    return _scan_where_group(query)[1]


def inject_values_block(query: str, block: str) -> str:
    _, where_at, group_open = _scan_where_group(query)
    if where_at is None:
        raise BindingError("query has no WHERE clause to bind")
    if group_open is None:
        raise BindingError("query WHERE clause has no group graph pattern")
    return f"{query[: group_open + 1]} {block} {query[group_open + 1 :]}"


def _serialize_term(value: str, param_type: ParamTypeName) -> str:
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
    """Return *template* rewritten to bind *bound* via an injected ``VALUES`` block."""
    if not bound:
        return template

    block = _values_block(bound, decls)
    first_brace, where_at, group_open = _scan_where_group(template)
    if where_at is None:
        raise BindingError("view template has no WHERE clause to parameterize")
    if group_open is None:
        raise BindingError("view template WHERE clause has no group graph pattern")
    if first_brace is None or where_at < first_brace:
        raise BindingError(
            "CONSTRUCT WHERE shorthand cannot carry parameters; "
            "use the explicit CONSTRUCT { ... } WHERE { ... } form"
        )

    rewritten = f"{template[: group_open + 1]} {block} {template[group_open + 1 :]}"
    try:
        parseQuery(rewritten)
    except ParseException as exc:
        raise BindingError(f"parameterized view template did not parse: {exc}") from exc
    return rewritten
