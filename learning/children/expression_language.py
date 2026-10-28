import ast
from dataclasses import field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from learning.children.unit_context import UnitContext
from learning.children.variables import VARIABLES
from model.timeline.fields import canonical_field


def _fn_ertek(u: UnitContext, name: Any) -> str:
    """Az almérés-nyelv „ertek” függvény: ertek("mező"): a mező értéke az eset végén.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), expression_language._fn_ures."""
    return u.timeline.value_at(canonical_field(name), u.end)

def _fn_ertek_kezdetkor(u: UnitContext, name: Any) -> str:
    """Az almérés-nyelv „ertek_kezdetkor” függvény: ertek_kezdetkor("mező"): a mező értéke az eset kezdetén.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.timeline.value_at(canonical_field(name), u.start)

def _fn_kitoltve(u: UnitContext, name: Any) -> bool:
    """Az almérés-nyelv „kitoltve” függvény: kitoltve("mező"): az eset alatt (szakasznál a csoport maga) kitöltötte a
    mezőt.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    key = canonical_field(name)
    return any(c.field == key and c.new for c in u.own_changes())

def _fn_valtozasok(u: UnitContext, name: Any) -> int:
    """Az almérés-nyelv „valtozasok” függvény: valtozasok("mező"): hányszor változott a mező az eset alatt.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    key = canonical_field(name)
    return sum(1 for c in u.own_changes() if c.field == key)

def _fn_ures(u: UnitContext, name: Any) -> bool:
    """Az almérés-nyelv „ures” függvény: ures("mező"): a mező üres az eset végén.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _fn_ertek(u, name) == ""

def _fn_szam(u: UnitContext, value: Any) -> Optional[float]:
    """Az almérés-nyelv „szam” függvény: szam(x): számmá alakít (pl. szam(ertek("reopen_count")) > 0).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _number(value)

FUNCTIONS: Dict[str, Tuple[Callable[..., Any], str]] = {
    "ertek": (_fn_ertek, 'ertek("mező"): a mező értéke az eset végén'),
    "ertek_kezdetkor": (_fn_ertek_kezdetkor, 'ertek_kezdetkor("mező"): a mező értéke az eset kezdetén'),
    "kitoltve": (_fn_kitoltve, 'kitoltve("mező"): az eset alatt (szakasznál a csoport maga) kitöltötte a mezőt'),
    "valtozasok": (_fn_valtozasok, 'valtozasok("mező"): hányszor változott a mező az eset alatt'),
    "ures": (_fn_ures, 'ures("mező"): a mező üres az eset végén'),
    "szam": (_fn_szam, 'szam(x): számmá alakít (pl. szam(ertek("reopen_count")) > 0)'),
}

FIELD_FUNCTIONS = frozenset({"ertek", "ertek_kezdetkor", "kitoltve", "valtozasok", "ures"})   # mezőnevet várnak

def vocabulary() -> Dict[str, Any]:
    """A szókincs a felületnek (súgó).

    Hívja: DiscoveryService.children_overview."""
    return {
        "variables": [{"name": k, "description": d} for k, (_, d) in VARIABLES.items()],
        "functions": [{"name": k, "description": d} for k, (_, d) in FUNCTIONS.items()],
        "operators": "and, or, not, <, <=, >, >=, ==, !=, in, not in, +, -, *, /; szöveg idézőjelben, lista [..]",
        "example": 'szakasz_perc < 15 and lezarta and kitoltve("close_notes")',
    }

_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare,
            ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Name, ast.Load,
            ast.Constant, ast.Call, ast.List, ast.Tuple, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div)

def compile_expression(source: str) -> ast.Expression:
    """A kifejezés ellenőrzése; hibánál ValueError érthető üzenettel.

    Hívja: DiscoveryManager.add_child, DiscoveryService.children_action, child_runner._run_group,
    child_runner.derived_evaluator, expression_language.rows_to_expression, child_runner.run_child."""
    text = str(source or "").strip()
    if not text:
        raise ValueError("Üres kifejezés.")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"Nem értelmezhető kifejezés ({error.msg}, {error.offset}. karakter).") from None
    # a függvények mezőnév-argumentuma idézőjel nélkül is jó: valtozasok(assignment_group)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FIELD_FUNCTIONS:
            node.args = [ast.copy_location(ast.Constant(arg.id), arg)
                         if isinstance(arg, ast.Name) and arg.id not in VARIABLES else arg for arg in node.args]
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"Nem megengedett elem a kifejezésben: {type(node).__name__}.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                name = node.func.id if isinstance(node.func, ast.Name) else "?"
                raise ValueError(f"Ismeretlen függvény: {name} (lehetséges: {', '.join(FUNCTIONS)}).")
        elif isinstance(node, ast.Name) and node.id not in VARIABLES and node.id not in FUNCTIONS \
                and node.id not in ("True", "False"):
            raise ValueError(f"Ismeretlen név: {node.id} (lehetséges: {', '.join(VARIABLES)}).")
        elif isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, float, bool)) and node.value is not None:
            raise ValueError("Csak szöveg, szám és igaz/hamis érték adható meg.")
    return tree

def _number(value: Any) -> Optional[float]:
    """Számmá alakít, ha lehet (különben None).

    Hívja: expression_language._compare, expression_language._evaluate, expression_language._fn_szam."""
    if isinstance(value, bool) or value is None:
        return None if value is None else float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None

def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    """Két érték összehasonlítása egy relációval (számként, ha mindkettő az; különben szövegként).

    Hívja: expression_language._evaluate."""
    if isinstance(op, (ast.In, ast.NotIn)):
        container = right if isinstance(right, (list, tuple)) else [right]
        values = {str(v) for v in container}
        found = str(left) in values or (_number(left) is not None and _number(left) in {_number(v) for v in container})
        return found if isinstance(op, ast.In) else not found
    if isinstance(op, (ast.Eq, ast.NotEq)):
        same = left == right or (str(left) == str(right) and left is not None and right is not None)
        if not same and _number(left) is not None and _number(right) is not None:
            same = _number(left) == _number(right)
        return same if isinstance(op, ast.Eq) else not same
    a, b = _number(left), _number(right)
    if a is None or b is None:
        return False   # hiányzó érték (pl. nem lett felelős) nem kisebb és nem nagyobb semminél
    return {ast.Lt: a < b, ast.LtE: a <= b, ast.Gt: a > b, ast.GtE: a >= b}[type(op)]

def _evaluate(node: ast.AST, unit: UnitContext) -> Any:
    """A kifejezésfa egy csomópontjának kiértékelése egy eseten (saját, biztonságos értelmező: csak a megengedett
    műveletek, változók és függvények).

    Hívja: expression_language.evaluate, expression_language.evaluate_value."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, unit)
    if isinstance(node, ast.BoolOp):
        values = (bool(_evaluate(v, unit)) for v in node.values)
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, unit)
        if isinstance(node.op, ast.Not):
            return not value
        number = _number(value)
        return None if number is None else -number
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, unit)
        for op, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, unit)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):
        a, b = _number(_evaluate(node.left, unit)), _number(_evaluate(node.right, unit))
        if a is None or b is None or (isinstance(node.op, ast.Div) and b == 0):
            return None
        return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b, ast.Div: a / b if b else None}[type(node.op)]
    if isinstance(node, ast.Name):
        if node.id in ("True", "False"):
            return node.id == "True"
        return VARIABLES[node.id][0](unit)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_evaluate(e, unit) for e in node.elts]
    if isinstance(node, ast.Call):
        return FUNCTIONS[node.func.id][0](unit, *[_evaluate(a, unit) for a in node.args])
    raise ValueError(f"Nem megengedett elem: {type(node).__name__}")

def evaluate(tree: ast.Expression, unit: UnitContext) -> bool:
    """Egy lefordított kifejezés igaz-e az esetre (ellenőrző almérés, keresési szempont).

    Hívja: child_runner.derived_evaluator, child_runner.measure_units."""
    return bool(_evaluate(tree, unit))

def evaluate_value(tree: ast.Expression, unit: UnitContext) -> str:
    """Csoportosításhoz: a kifejezés értéke szövegként (üres érték: "").

    Hívja: child_runner.derived_evaluator, child_runner.measure_groups."""
    value = _evaluate(tree, unit)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "igen" if value else "nem"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)

METHODS = {"check": "ellenőrzés", "group": "csoportosítás"}

GROUP_ROWS = 5

_ROW_OPS = {"<", "<=", ">", ">=", "==", "!=", "in", "not in"}

def rows_to_expression(rows: Sequence[Any]) -> str:
    """Adatsor -> kifejezés. Elemei: feltétel ({"field"|"var"|"fn"+"arg", "op", "value", "not"}) vagy
    "and" / "or" kapcsoló (két feltétel között; alapból "and").

    Hívja: DiscoveryService.children_action."""
    parts: List[str] = []
    for row in rows:
        if isinstance(row, str):
            if row.strip().lower() not in ("and", "or"):
                raise ValueError(f"Ismeretlen kapcsoló: {row} (and | or).")
            parts.append(row.strip().lower())
            continue
        if parts and parts[-1] not in ("and", "or"):
            parts.append("and")
        if "field" in row:
            subject = f"ertek({row['field']!r})"
        elif "fn" in row:
            subject = f"{row['fn']}({row.get('arg', '')!r})"
        else:
            subject = str(row.get("var", ""))
        op = str(row.get("op", "")).strip()
        if op:
            if op not in _ROW_OPS:
                raise ValueError(f"Ismeretlen művelet: {op}.")
            value = row.get("value")
            text = f"{subject} {op} {value!r}"
        else:
            text = subject
        parts.append(f"not ({text})" if row.get("not") else f"({text})")
    expression = " ".join(parts)
    compile_expression(expression)
    return expression
