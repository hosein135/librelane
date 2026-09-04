"""Extract the port list of a Verilog module (ANSI or non-ANSI style).

Used to generate harness wrappers (Tiny Tapeout ``tt_um_*``, Caravel
``user_project_wrapper``, wafer.space ``chip_core``). The gate-level netlist
written by LibreLane is parsed first because every port there has a constant
width; the uploaded RTL is the fallback.
"""

from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_ATTRIBUTE_RE = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_IDENT = r"[A-Za-z_][A-Za-z0-9_$]*"
_DIRECTIONS = ("input", "output", "inout")
_NET_KINDS = (
    "wire",
    "reg",
    "logic",
    "tri",
    "tri0",
    "tri1",
    "wand",
    "wor",
    "supply0",
    "supply1",
    "var",
    "bit",
    "integer",
)

CLOCK_NAMES = {
    "clk",
    "clock",
    "clk_i",
    "i_clk",
    "clk_in",
    "clkin",
    "sys_clk",
    "sysclk",
    "clock_i",
    "wb_clk_i",
    "core_clk",
}
RESET_LOW_NAMES = {
    "rst_n",
    "rstn",
    "reset_n",
    "resetn",
    "nrst",
    "nreset",
    "rst_ni",
    "arst_n",
    "aresetn",
    "areset_n",
    "i_rst_n",
    "rst_n_i",
    "resetn_i",
    "sys_rst_n",
    "rst_b",
    "reset_b",
}
RESET_HIGH_NAMES = {
    "rst",
    "reset",
    "rst_i",
    "i_rst",
    "arst",
    "areset",
    "sys_rst",
    "reset_i",
    "wb_rst_i",
    "srst",
    "hard_reset",
}


@dataclass
class Port:
    name: str
    direction: str  # input | output | inout
    msb: int | None = None
    lsb: int | None = None
    width_known: bool = True

    @property
    def is_vector(self) -> bool:
        return self.msb is not None and self.lsb is not None

    @property
    def width(self) -> int:
        if not self.is_vector:
            return 1
        assert self.msb is not None and self.lsb is not None
        return abs(self.msb - self.lsb) + 1

    @property
    def range_decl(self) -> str:
        """``[7:0]`` (with trailing space) or ``""`` for scalars."""
        if not self.is_vector:
            return ""
        return f"[{self.msb}:{self.lsb}] "

    def bit_indices(self) -> list[int | None]:
        """Indices from LSB to MSB, or ``[None]`` for a scalar."""
        if not self.is_vector:
            return [None]
        assert self.msb is not None and self.lsb is not None
        lo, hi = sorted((self.msb, self.lsb))
        return list(range(lo, hi + 1))

    def select(self, index: int | None) -> str:
        return self.name if index is None else f"{self.name}[{index}]"


@dataclass
class ModulePorts:
    module: str
    ports: list[Port]
    style: str  # "ansi" | "nonansi"
    warnings: list[str]

    def inputs(self) -> list[Port]:
        return [p for p in self.ports if p.direction == "input"]

    def outputs(self) -> list[Port]:
        return [p for p in self.ports if p.direction == "output"]

    def inouts(self) -> list[Port]:
        return [p for p in self.ports if p.direction == "inout"]

    def bit_count(self, direction: str) -> int:
        return sum(p.width for p in self.ports if p.direction == direction)


def strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    text = _LINE_COMMENT_RE.sub(" ", text)
    return _ATTRIBUTE_RE.sub(" ", text)


def _balanced(text: str, pos: int, open_ch: str, close_ch: str) -> tuple[str, int] | None:
    """Return (inner, index_after_close) for the group starting at ``pos``."""
    if pos >= len(text) or text[pos] != open_ch:
        return None
    depth = 0
    for i in range(pos, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[pos + 1 : i], i + 1
    return None


def split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


# --- Constant expression evaluation (for parameterised widths) -------------

_SIZED_LITERAL_RE = re.compile(
    r"(?:(\d[\d_]*)\s*)?'\s*[sS]?([bBoOdDhH])\s*([0-9a-fA-F_xXzZ?]+)"
)
_DIGIT_UNDERSCORE_RE = re.compile(r"(?<=\d)_(?=\d)")
_BASES = {"b": 2, "o": 8, "d": 10, "h": 16}


def _sized_literal(match: re.Match) -> str:
    base = _BASES[match.group(2).lower()]
    digits = match.group(3).replace("_", "")
    digits = re.sub(r"[xXzZ?]", "0", digits)
    try:
        return str(int(digits, base))
    except ValueError:
        return "0"


class _SafeEval(ast.NodeVisitor):
    def __init__(self, params: dict[str, int]) -> None:
        self.params = params

    def visit(self, node):  # type: ignore[override]
        if isinstance(node, ast.Expression):
            return self.visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return int(node.value)
        if isinstance(node, ast.Name):
            if node.id in self.params:
                return int(self.params[node.id])
            raise ValueError(f"unknown parameter {node.id}")
        if isinstance(node, ast.UnaryOp):
            value = self.visit(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.Invert):
                return ~value
            raise ValueError("unsupported unary operator")
        if isinstance(node, ast.BinOp):
            left = self.visit(node.left)
            right = self.visit(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, (ast.Div, ast.FloorDiv)):
                if right == 0:
                    raise ValueError("division by zero")
                return left // right
            if isinstance(op, ast.Mod):
                if right == 0:
                    raise ValueError("division by zero")
                return left % right
            if isinstance(op, ast.Pow):
                if right < 0 or right > 64:
                    raise ValueError("unsupported exponent")
                return left**right
            if isinstance(op, ast.LShift):
                return left << min(right, 64)
            if isinstance(op, ast.RShift):
                return left >> min(right, 64)
            if isinstance(op, ast.BitOr):
                return left | right
            if isinstance(op, ast.BitAnd):
                return left & right
            if isinstance(op, ast.BitXor):
                return left ^ right
            raise ValueError("unsupported binary operator")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            args = [self.visit(a) for a in node.args]
            if node.func.id == "clog2" and len(args) == 1:
                return 0 if args[0] <= 1 else math.ceil(math.log2(args[0]))
            if node.func.id in ("max", "min") and args:
                return max(args) if node.func.id == "max" else min(args)
            raise ValueError(f"unsupported function {node.func.id}")
        raise ValueError(f"unsupported syntax {type(node).__name__}")


def eval_int_expr(expr: str, params: dict[str, int] | None = None) -> int | None:
    """Evaluate a constant Verilog expression; ``None`` when not statically known."""
    text = expr.strip()
    if not text:
        return None
    text = _SIZED_LITERAL_RE.sub(_sized_literal, text)
    text = _DIGIT_UNDERSCORE_RE.sub("", text)
    text = text.replace("$clog2", "clog2")
    try:
        tree = ast.parse(text, mode="eval")
        return int(_SafeEval(params or {}).visit(tree))
    except (SyntaxError, ValueError, TypeError, RecursionError, OverflowError):
        return None


_PARAM_ITEM_RE = re.compile(
    r"^(?:(?:parameter|localparam)\s+)?"
    r"(?:(?:integer|int|real|time|logic|bit|reg|signed|unsigned|\[[^\]]*\])\s+)*"
    rf"(?P<name>{_IDENT})\s*=\s*(?P<value>.+)$",
    re.DOTALL,
)


def collect_parameters(header_params: str, body: str) -> dict[str, int]:
    params: dict[str, int] = {}

    def absorb(chunk: str) -> None:
        for item in split_top_level(chunk):
            m = _PARAM_ITEM_RE.match(item.strip())
            if not m:
                continue
            value = eval_int_expr(m.group("value"), params)
            if value is not None:
                params[m.group("name")] = value

    if header_params:
        absorb(header_params)
    for m in re.finditer(r"\b(?:parameter|localparam)\b([^;]*);", body):
        absorb("parameter " + m.group(1))
    return params


# --- Port declaration parsing ----------------------------------------------

_RANGE_RE = re.compile(r"\[([^\]]+)\]")


def _parse_ranges(range_text: str, params: dict[str, int]) -> tuple[int | None, int | None, bool]:
    """Return (msb, lsb, known). Multi-dimensional packed ranges are flattened."""
    ranges = _RANGE_RE.findall(range_text or "")
    if not ranges:
        return None, None, True
    if len(ranges) == 1:
        if ":" not in ranges[0]:
            size = eval_int_expr(ranges[0], params)  # SystemVerilog [N] == [N-1:0]
            if size is None or size <= 0:
                return None, None, False
            return size - 1, 0, True
        left, right = ranges[0].split(":", 1)
        msb = eval_int_expr(left, params)
        lsb = eval_int_expr(right, params)
        if msb is None or lsb is None:
            return None, None, False
        return msb, lsb, True
    total = 1
    for rng in ranges:
        if ":" in rng:
            left, right = rng.split(":", 1)
            a = eval_int_expr(left, params)
            b = eval_int_expr(right, params)
            if a is None or b is None:
                return None, None, False
            total *= abs(a - b) + 1
        else:
            size = eval_int_expr(rng, params)
            if size is None or size <= 0:
                return None, None, False
            total *= size
    return total - 1, 0, True


_ANSI_PORT_RE = re.compile(
    r"^(?:(?P<dir>input|output|inout)\s+)?"
    r"(?:(?P<kind>" + "|".join(_NET_KINDS) + r")\s+)?"
    r"(?:(?P<sign>signed|unsigned)\s+)?"
    r"(?P<range>(?:\[[^\]]+\]\s*)*)"
    rf"(?P<name>{_IDENT})"
    r"(?P<unpacked>(?:\s*\[[^\]]+\])*)"
    r"(?:\s*=\s*.*)?$",
    re.DOTALL,
)

_BODY_DECL_RE = re.compile(
    r"\b(?P<dir>input|output|inout)\b"
    r"(?:\s+(?:" + "|".join(_NET_KINDS) + r"))?"
    r"(?:\s+(?:signed|unsigned))?"
    r"\s*(?P<range>(?:\[[^\]]+\]\s*)*)"
    rf"(?P<names>{_IDENT}(?:\s*,\s*{_IDENT})*)\s*;"
)


def find_module_header(text: str, module: str) -> tuple[str, str, str] | None:
    """Return (parameter_list, port_list, body) of ``module`` or ``None``."""
    m = re.search(rf"\bmodule\s+{re.escape(module)}\b", text)
    if not m:
        return None
    pos = m.end()
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    params = ""
    if pos < n and text[pos] == "#":
        pos += 1
        while pos < n and text[pos].isspace():
            pos += 1
        group = _balanced(text, pos, "(", ")")
        if group is None:
            return None
        params, pos = group
        while pos < n and text[pos].isspace():
            pos += 1
    ports = ""
    if pos < n and text[pos] == "(":
        group = _balanced(text, pos, "(", ")")
        if group is None:
            return None
        ports, pos = group
    semi = text.find(";", pos)
    if semi < 0:
        return None
    end = re.compile(r"\bendmodule\b").search(text, semi)
    body = text[semi + 1 : end.start() if end else len(text)]
    return params, ports, body


def parse_module_ports(source: str, module: str) -> ModulePorts | None:
    """Parse the ports of ``module`` from Verilog/SystemVerilog ``source``."""
    text = strip_comments(source)
    header = find_module_header(text, module)
    if header is None:
        return None
    params_text, ports_text, body = header
    params = collect_parameters(params_text, body)
    warnings: list[str] = []
    ports: list[Port] = []

    items = split_top_level(ports_text)
    ansi = any(re.match(r"^(input|output|inout)\b", item) for item in items)

    if ansi:
        current_dir: str | None = None
        current_range = ""
        for item in items:
            m = _ANSI_PORT_RE.match(item)
            if not m:
                warnings.append(f"Could not parse port declaration: {item!r}")
                continue
            if m.group("dir"):
                current_dir = m.group("dir")
                current_range = m.group("range") or ""
            elif m.group("range"):
                current_range = m.group("range")
            if current_dir is None:
                warnings.append(f"Port {m.group('name')!r} has no direction; skipped.")
                continue
            if m.group("unpacked").strip():
                warnings.append(
                    f"Port {m.group('name')!r} is an unpacked array; harness wrappers cannot map it."
                )
            msb, lsb, known = _parse_ranges(current_range, params)
            if not known:
                warnings.append(
                    f"Width of port {m.group('name')!r} ({current_range.strip()}) is not a constant; treated as 1 bit."
                )
            ports.append(Port(m.group("name"), current_dir, msb, lsb, known))
        return ModulePorts(module, ports, "ansi", warnings)

    names: list[str] = []
    for item in items:
        dotted = re.match(rf"^\.\s*({_IDENT})\s*\(", item)
        if dotted:
            names.append(dotted.group(1))
            continue
        ident = re.match(rf"^({_IDENT})$", item)
        if ident:
            names.append(ident.group(1))
        else:
            warnings.append(f"Unrecognised entry in port list: {item!r}")
    declared: dict[str, Port] = {}
    for m in _BODY_DECL_RE.finditer(body):
        msb, lsb, known = _parse_ranges(m.group("range"), params)
        for name in re.split(r"\s*,\s*", m.group("names").strip()):
            if name in declared:
                continue
            if not known:
                warnings.append(
                    f"Width of port {name!r} ({m.group('range').strip()}) is not a constant; treated as 1 bit."
                )
            declared[name] = Port(name, m.group("dir"), msb, lsb, known)
    for name in names:
        port = declared.get(name)
        if port is None:
            warnings.append(f"Port {name!r} is listed but never declared with a direction; skipped.")
            continue
        ports.append(port)
    return ModulePorts(module, ports, "nonansi", warnings)


# --- Clock / reset classification -------------------------------------------


def looks_like_clock(port: Port, preferred: str | None = None) -> bool:
    if port.direction != "input" or port.width != 1:
        return False
    name = port.name.lower()
    if preferred and port.name == preferred:
        return True
    return name in CLOCK_NAMES or name.endswith("_clk") or name.startswith("clk_")


def reset_polarity(port: Port) -> str | None:
    """``"low"`` / ``"high"`` for reset-like scalar inputs, else ``None``."""
    if port.direction != "input" or port.width != 1:
        return None
    name = port.name.lower()
    if name in RESET_LOW_NAMES:
        return "low"
    if name in RESET_HIGH_NAMES:
        return "high"
    if "rst" in name or "reset" in name:
        if name.endswith(("_n", "n_i", "_ni", "_b")) or name.startswith("n"):
            return "low"
        return "high"
    return None
