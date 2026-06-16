from pathlib import Path

from tree_sitter import Language, Parser

from agent.indexing.models import Reference, Symbol


def get_parser() -> Parser:
    import tree_sitter_python as tspython

    language = Language(tspython.language())
    parser = Parser(language)
    return parser


PYTHON_KIND_MAP = {
    "function_definition": "function",
    "class_definition": "class",
}


def parse_file(file_path: str) -> tuple[list[Symbol], list[Reference]]:
    parser = get_parser()
    source_bytes = Path(file_path).read_bytes()
    tree = parser.parse(source_bytes)

    symbols: list[Symbol] = []
    references: list[Reference] = []
    root = tree.root_node
    rel_path = _relative_path(file_path)

    def visit(node, scope: str | None = None):
        kind = PYTHON_KIND_MAP.get(node.type)
        if kind and node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                symbols.append(
                    Symbol(
                        path=rel_path,
                        name=name,
                        kind=kind,
                        line=name_node.start_point[0] + 1,
                        column=name_node.start_point[1],
                        scope=scope,
                    )
                )
                new_scope = f"{scope}.{name}" if scope else name
                for child in node.children:
                    visit(child, new_scope)
                return

        if node.type == "identifier":
            name = node.text.decode("utf-8")
            references.append(
                Reference(
                    path=rel_path,
                    name=name,
                    line=node.start_point[0] + 1,
                    column=node.start_point[1],
                )
            )

        for child in node.children:
            visit(child, scope)

    visit(root)
    return symbols, references


def _relative_path(file_path: str) -> str:
    cwd = Path.cwd()
    try:
        return str(Path(file_path).relative_to(cwd))
    except ValueError:
        return file_path


def parse_workspace(workspace: str) -> tuple[list[Symbol], list[Reference]]:
    all_symbols: list[Symbol] = []
    all_refs: list[Reference] = []
    ws_path = Path(workspace)
    for py_file in ws_path.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        symbols, refs = parse_file(str(py_file))
        all_symbols.extend(symbols)
        all_refs.extend(refs)
    return all_symbols, all_refs
