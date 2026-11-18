from __future__ import annotations
import argparse
import enum
import json
import logging
import math
import struct
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Union

Number = Union[int, float]
logger = logging.getLogger("math_driver")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class ErrorCode(enum.Enum):
    UNKNOWN = "UNKNOWN"
    BAD_TYPE = "BAD_TYPE"
    BAD_DOMAIN = "BAD_DOMAIN"
    BAD_ARGUMENT = "BAD_ARGUMENT"
    OVERFLOW = "OVERFLOW"
    UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
    UNSUPPORTED_IN_VERSION = "UNSUPPORTED_IN_VERSION"


class MathError(ValueError):
    default_code: ErrorCode = ErrorCode.UNKNOWN

    def __init__(self, message: str, *, code: Optional[ErrorCode] = None,
                 operation: Optional[str] = None,
                 details: Optional[dict] = None) -> None:
        self.message = message
        self.code = code or self.default_code
        self.operation = operation
        self.details = dict(details or {})
        super().__init__(self._format())

    def _format(self) -> str:
        parts = [f"[{self.code.value}]"]
        if self.operation:
            parts.append(f"{self.operation}:")
        parts.append(self.message)
        if self.details:
            parts.append("(" + ", ".join(f"{k}={v!r}" for k, v in self.details.items()) + ")")
        return " ".join(parts)

    def __str__(self) -> str:
        return self._format()

    def __repr__(self) -> str:
        return (f"{type(self).__name__}(code={self.code.value!r}, "
                f"operation={self.operation!r}, message={self.message!r}, "
                f"details={self.details!r})")

    def to_dict(self) -> dict:
        return {"type": type(self).__name__, "code": self.code.value,
                "operation": self.operation, "message": self.message,
                "details": self.details}

    def to_json(self, **kwargs) -> str:
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kwargs)

    def __reduce__(self):
        return (_rebuild_math_error, (type(self), self.message, self.code,
                                      self.operation, self.details))

    @classmethod
    def bad_type(cls, name: str, value, expected: str = "int или float") -> "MathError":
        return cls(f"неверный тип аргумента '{name}': ожидается {expected}",
                   code=ErrorCode.BAD_TYPE,
                   details={"arg": name, "got": type(value).__name__,
                            "expected": expected})

    @classmethod
    def bad_domain(cls, operation: str, message: str, **details) -> "MathError":
        return cls(message, code=ErrorCode.BAD_DOMAIN,
                   operation=operation, details=details)

    @classmethod
    def bad_argument(cls, operation: str, message: str, **details) -> "MathError":
        return cls(message, code=ErrorCode.BAD_ARGUMENT,
                   operation=operation, details=details)

    @classmethod
    def overflow(cls, operation: str, message: str, **details) -> "MathError":
        return cls(message, code=ErrorCode.OVERFLOW,
                   operation=operation, details=details)

class UnsupportedInVersion(MathError):
    default_code = ErrorCode.UNSUPPORTED_IN_VERSION

    def __init__(self, function: str, target_version: str, min_version: str, *,
                 polyfill_available: bool = False,
                 extra: Optional[dict] = None) -> None:
        self.function = function
        self.target_version = target_version
        self.min_version = min_version
        self.polyfill_available = polyfill_available
        message = (f"функция '{function}' недоступна в Python {target_version} "
                   f"(появилась в {min_version})")
        if polyfill_available:
            message += "; полифил существует, но отключён для этой версии"
        details = {"function": function, "target_version": target_version,
                   "min_version": min_version,
                   "polyfill_available": polyfill_available}
        if extra:
            details.update(extra)
        super().__init__(message, code=ErrorCode.UNSUPPORTED_IN_VERSION,
                         operation=function, details=details)

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update({"function": self.function,
                  "target_version": self.target_version,
                  "min_version": self.min_version,
                  "polyfill_available": self.polyfill_available})
        return d

    def version_gap(self) -> Tuple[int, int, int]:
        t = tuple(int(x) for x in self.target_version.split("."))
        m = tuple(int(x) for x in self.min_version.split("."))
        return tuple(max(0, m[i] - t[i]) for i in range(3))  

    def suggest_version(self) -> str:
        return self.min_version

def _rebuild_math_error(cls, message, code, operation, details):
    obj = cls.__new__(cls)
    MathError.__init__(obj, message, code=code, operation=operation,
                       details=details)
    return obj

def _parse_version(s: str) -> Tuple[int, int, int]:
    parts = s.strip().split(".")
    if not (1 <= len(parts) <= 3):
        raise argparse.ArgumentTypeError(f"Некорректная версия: {s}")
    try:
        nums = [int(p) for p in parts] + [0] * (3 - len(parts))
    except ValueError:
        raise argparse.ArgumentTypeError(f"Некорректная версия: {s}")
    if nums[0] != 3:
        raise argparse.ArgumentTypeError("Поддерживается только Python 3.x")
    if not (0 <= nums[1] <= 15) or not (0 <= nums[2] <= 99):
        raise argparse.ArgumentTypeError(f"Версия вне поддерживаемого диапазона: {s}")
    return (nums[0], nums[1], nums[2])

def _ver(v: str) -> Tuple[int, int, int]:
    return _parse_version(v)

def _polyfill_cbrt(x: float) -> float:
    return math.copysign(abs(x) ** (1.0 / 3.0), x)

def _polyfill_exp2(x: float) -> float:
    return 2.0 ** x

def _polyfill_lcm(*args: int) -> int:
    if not args:
        raise MathError.bad_argument("lcm", "нужен хотя бы один аргумент")
    result = 1
    for a in args:
        a = abs(int(a))
        if a == 0:
            return 0
        g = math.gcd(result, a) or 1
        result = result // g * a
    return result

def _polyfill_nextafter(x: float, y: float) -> float:
    if math.isnan(x) or math.isnan(y):
        return x + y
    if x == y:
        return y
    if x == 0.0:
        smallest = struct.unpack("<d", struct.pack("<Q", 1))[0]
        return smallest if y > 0 else -smallest
    eps = math.ulp(x) if hasattr(math, "ulp") else abs(x) * sys.float_info.epsilon
    return x + (eps if y > x else -eps)

def _polyfill_sumprod(p, q):
    if len(p) != len(q):
        raise MathError.bad_argument("sumprod", "длины последовательностей должны совпадать",
                                     len_p=len(p), len_q=len(q))
    return math.fsum(a * b for a, b in zip(p, q))

def _polyfill_fma(x: float, y: float, z: float) -> float:
    return math.fsum([x * y, z])

def _polyfill_ulp(x: float) -> float:
    if math.isnan(x):
        return x
    if math.isinf(x):
        return math.inf
    if x == 0.0:
        return struct.unpack("<d", struct.pack("<Q", 1))[0]
    if hasattr(math, "ulp"):
        return math.ulp(x)
    return abs(x) * sys.float_info.epsilon

def _polyfill_sinpi(x: float) -> float:
    return math.sin(math.pi * x)

def _polyfill_cospi(x: float) -> float:
    return math.cos(math.pi * x)

def _polyfill_tanpi(x: float) -> float:
    return math.tan(math.pi * x)


FunctionSpec = Tuple[Tuple[int, int, int], Optional[Callable]]

REGISTRY: Dict[str, FunctionSpec] = {
    "ceil":      (_ver("3.0"), math.ceil),
    "copysign":  (_ver("3.0"), math.copysign),
    "fabs":      (_ver("3.0"), math.fabs),
    "factorial": (_ver("3.0"), math.factorial),
    "floor":     (_ver("3.0"), math.floor),
    "fmod":      (_ver("3.0"), math.fmod),
    "frexp":     (_ver("3.0"), math.frexp),
    "fsum":      (_ver("3.0"), math.fsum),
    "gcd":       (_ver("3.0"), math.gcd),
    "isinf":     (_ver("3.0"), math.isinf),
    "isnan":     (_ver("3.0"), math.isnan),
    "ldexp":     (_ver("3.0"), math.ldexp),
    "modf":      (_ver("3.0"), math.modf),
    "trunc":     (_ver("3.0"), math.trunc),
    "exp":       (_ver("3.0"), math.exp),
    "expm1":     (_ver("3.0"), getattr(math, "expm1", None)),
    "log":       (_ver("3.0"), math.log),
    "log1p":     (_ver("3.0"), math.log1p),
    "log2":      (_ver("3.0"), math.log2),
    "log10":     (_ver("3.0"), math.log10),
    "pow":       (_ver("3.0"), math.pow),
    "sqrt":      (_ver("3.0"), math.sqrt),
    "acos":      (_ver("3.0"), math.acos),
    "asin":      (_ver("3.0"), math.asin),
    "atan":      (_ver("3.0"), math.atan),
    "atan2":     (_ver("3.0"), math.atan2),
    "cos":       (_ver("3.0"), math.cos),
    "sin":       (_ver("3.0"), math.sin),
    "tan":       (_ver("3.0"), math.tan),
    "hypot":     (_ver("3.0"), math.hypot),
    "degrees":   (_ver("3.0"), math.degrees),
    "radians":   (_ver("3.0"), math.radians),
    "acosh":     (_ver("3.0"), math.acosh),
    "asinh":     (_ver("3.0"), math.asinh),
    "atanh":     (_ver("3.0"), math.atanh),
    "cosh":      (_ver("3.0"), math.cosh),
    "sinh":      (_ver("3.0"), math.sinh),
    "tanh":      (_ver("3.0"), math.tanh),
    "erf":       (_ver("3.0"), math.erf),
    "erfc":      (_ver("3.0"), math.erfc),
    "gamma":     (_ver("3.0"), math.gamma),
    "lgamma":    (_ver("3.0"), math.lgamma),
    "isfinite":  (_ver("3.2"), math.isfinite),
    "isclose":   (_ver("3.5"), math.isclose),
    "dist":      (_ver("3.8"), getattr(math, "dist", None)),
    "perm":      (_ver("3.8"), getattr(math, "perm", None)),
    "comb":      (_ver("3.8"), getattr(math, "comb", None)),
    "prod":      (_ver("3.8"), getattr(math, "prod", None)),
    "isqrt":     (_ver("3.8"), math.isqrt),
    "lcm":       (_ver("3.9"), getattr(math, "lcm", None)),
    "nextafter": (_ver("3.9"), getattr(math, "nextafter", None)),
    "ulp":       (_ver("3.9"), getattr(math, "ulp", None)),
    "cbrt":      (_ver("3.11"), getattr(math, "cbrt", None)),
    "exp2":      (_ver("3.11"), getattr(math, "exp2", None)),
    "sumprod":   (_ver("3.12"), getattr(math, "sumprod", None)),
    "fma":       (_ver("3.13"), getattr(math, "fma", None)),
    "sinpi":     (_ver("3.15"), getattr(math, "sinpi", None)),
    "cospi":     (_ver("3.15"), getattr(math, "cospi", None)),
    "tanpi":     (_ver("3.15"), getattr(math, "tanpi", None)),
    "remainder": (_ver("3.0"), math.remainder),
}

POLYFILLS: Dict[str, Callable] = {
    "cbrt":      _polyfill_cbrt,
    "exp2":      _polyfill_exp2,
    "lcm":       _polyfill_lcm,
    "nextafter": _polyfill_nextafter,
    "sumprod":   _polyfill_sumprod,
    "fma":       _polyfill_fma,
    "ulp":       _polyfill_ulp,
    "sinpi":     _polyfill_sinpi,
    "cospi":     _polyfill_cospi,
    "tanpi":     _polyfill_tanpi,
}

@dataclass
class TargetVersion:
    major: int
    minor: int
    micro: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.micro}"

    def as_tuple(self) -> Tuple[int, int, int]:
        return (self.major, self.minor, self.micro)

    def __ge__(self, v: Tuple[int, int, int]) -> bool:
        return self.as_tuple() >= v

    def __lt__(self, v: Tuple[int, int, int]) -> bool:
        return self.as_tuple() < v

class MathDriver:
    PI = math.pi
    E = math.e
    TAU = getattr(math, "tau", 2 * math.pi)
    INF = math.inf
    NAN = math.nan

    def __init__(self, target: Union[str, Tuple[int, int, int], None] = None) -> None:
        if target is None:
            target = (3, sys.version_info.minor, sys.version_info.micro)
        if isinstance(target, str):
            target = _parse_version(target)
        self.target = TargetVersion(*target)
        self._available: Dict[str, Callable] = {}
        self._blocked: Dict[str, Tuple[Tuple[int, int, int], bool]] = {}
        self._build_table()
        logger.info("MathDriver target=%s, интерпретатор=%s.%s.%s, "
                    "доступно=%d, заблокировано=%d",
                    self.target, sys.version_info.major, sys.version_info.minor,
                    sys.version_info.micro, len(self._available),
                    len(self._blocked))

    def _build_table(self) -> None:
        for name, (min_ver, impl) in REGISTRY.items():
            if self.target < min_ver:
                self._blocked[name] = (min_ver, name in POLYFILLS)
                continue
            if impl is not None:
                self._available[name] = impl
            elif name in POLYFILLS:
                self._available[name] = POLYFILLS[name]
                logger.debug("Полифил для %s (target %s)", name, self.target)
            else:
                self._blocked[name] = (min_ver, False)

    def _resolve(self, name: str) -> Callable:
        if name in self._blocked:
            min_ver, has_polyfill = self._blocked[name]
            raise UnsupportedInVersion(function=name,
                                       target_version=str(self.target),
                                       min_version=".".join(map(str, min_ver)),
                                       polyfill_available=has_polyfill)
        if name not in self._available:
            raise MathError(f"неизвестная операция '{name}'",
                            code=ErrorCode.UNKNOWN_OPERATION)
        return self._available[name]

    @staticmethod
    def _check_number(x, name: str = "x") -> float:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise MathError.bad_type(name, x)
        return float(x)

    def supported(self) -> List[str]:
        return sorted(self._available)

    def unavailable(self) -> Dict[str, Tuple[Tuple[int, int, int], bool]]:
        return dict(self._blocked)

    def call(self, name: str, *args, **kwargs):
        fn = self._resolve(name)
        logger.info("call %s(%s, %s)", name, args, kwargs)
        try:
            return fn(*args, **kwargs)
        except MathError:
            raise
        except ValueError as e:
            raise MathError(str(e), code=ErrorCode.BAD_DOMAIN,
                            operation=name, details={"args": args})
        except OverflowError as e:
            raise MathError.overflow(name, str(e), args=args)
        except ZeroDivisionError as e:
            raise MathError.bad_domain(name, str(e), args=args)

    def sqrt(self, x):  return self.call("sqrt", self._check_number(x))
    def sin(self, x):   return self.call("sin", self._check_number(x))
    def cos(self, x):   return self.call("cos", self._check_number(x))
    def tan(self, x):   return self.call("tan", self._check_number(x))
    def asin(self, x):  return self.call("asin", self._check_number(x))
    def acos(self, x):  return self.call("acos", self._check_number(x))
    def atan(self, x):  return self.call("atan", self._check_number(x))
    def atan2(self, y, x): return self.call("atan2", self._check_number(y), self._check_number(x))
    def sinh(self, x):  return self.call("sinh", self._check_number(x))
    def cosh(self, x):  return self.call("cosh", self._check_number(x))
    def tanh(self, x):  return self.call("tanh", self._check_number(x))
    def exp(self, x):   return self.call("exp", self._check_number(x))
    def log(self, x, base=math.e): return self.call("log", self._check_number(x), base)
    def log10(self, x): return self.call("log10", self._check_number(x))
    def log2(self, x):  return self.call("log2", self._check_number(x))
    def pow(self, a, b):return self.call("pow", self._check_number(a), self._check_number(b))
    def hypot(self, *a):return self.call("hypot", *[self._check_number(v) for v in a])
    def dist(self, p, q): return self.call("dist", p, q)
    def cbrt(self, x):  return self.call("cbrt", self._check_number(x))
    def exp2(self, x):  return self.call("exp2", self._check_number(x))
    def lcm(self, *a):  return self.call("lcm", *a)
    def gcd(self, *a):  return self.call("gcd", *a)
    def factorial(self, n): return self.call("factorial", n)
    def comb(self, n, k):   return self.call("comb", n, k)
    def perm(self, n, k=None):
        if k is None:
            return self.call("perm", n)
        return self.call("perm", n, k)
    def prod(self, iterable, start=1): return self.call("prod", iterable, start)
    def sumprod(self, p, q): return self.call("sumprod", p, q)
    def fma(self, x, y, z):  return self.call("fma", x, y, z)
    def nextafter(self, x, y): return self.call("nextafter", x, y)
    def ulp(self, x):          return self.call("ulp", x)
    def degrees(self, x):      return self.call("degrees", self._check_number(x))
    def radians(self, x):      return self.call("radians", self._check_number(x))
    def isclose(self, a, b, rel_tol=1e-9, abs_tol=0.0):
        return self.call("isclose", a, b, rel_tol=rel_tol, abs_tol=abs_tol)

def _parse_argv_for_version() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Драйвер math с выбором целевой версии Python",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--py", "-p",
                        default=f"{sys.version_info.major}.{sys.version_info.minor}",
                        help="Целевая версия Python: 3.7, 3.11, 3.15.2")
    parser.add_argument("--list", "-l", action="store_true",
                        help="Показать доступные и заблокированные функции")
    parser.add_argument("--call", "-c", metavar="EXPR",
                        help="Вызов: 'sqrt 16' или 'sumprod [1,2,3] [4,5,6]'")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Вывод списка в JSON")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Подробный лог")
    return parser.parse_args()

def _eval_call(driver: MathDriver, expr: str):
    parts = expr.strip().split(None, 1)
    if not parts:
        raise MathError("пустое выражение", code=ErrorCode.BAD_ARGUMENT)
    name = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    args = []
    if rest.strip():
        for token in rest.split(","):
            token = token.strip()
            try:
                args.append(eval(token, {"__builtins__": {}}, {"math": math}))
            except Exception as e:
                raise MathError(f"не удалось разобрать аргумент '{token}': {e}",
                                code=ErrorCode.BAD_ARGUMENT,
                                operation=name,
                                details={"token": token})
    return driver.call(name, *args)

def main() -> int:
    args = _parse_argv_for_version()
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    try:
        target = _parse_version(args.py)
    except argparse.ArgumentTypeError as e:
        print(f"Ошибка версии: {e}", file=sys.stderr)
        return 2
    driver = MathDriver(target)
    if args.list:
        if args.json:
            payload = {"target": str(driver.target),
                       "interpreter": sys.version.split()[0],
                       "supported": driver.supported(),
                       "blocked": {n: {"min_version": ".".join(map(str, mv)),
                                       "polyfill": poly}
                                   for n, (mv, poly) in driver.unavailable().items()}}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Целевая версия: {driver.target} (интерпретатор: {sys.version.split()[0]})")
            print(f"\nДоступно ({len(driver.supported())}):")
            for n in driver.supported():
                print(f"  + {n}")
            print(f"\nЗаблокировано ({len(driver.unavailable())}):")
            for n, (mv, poly) in sorted(driver.unavailable().items()):
                marker = " [полифил есть]" if poly else ""
                print(f"  - {n}: появилась в {'.'.join(map(str, mv))}{marker}")
        return 0
    if args.call:
        try:
            print(_eval_call(driver, args.call))
        except MathError as e:
            print(e.to_json(indent=2), file=sys.stderr)
            return 1
        return 0
    print(f"MathDriver REPL. Целевая версия: {driver.target}. "
          f"Команды: list, exit, <имя> <args через запятую>")
    while True:
        try:
            line = input(f"[py{driver.target}]>>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "list":
            print("Доступно:", ", ".join(driver.supported()))
            continue
        try:
            print(_eval_call(driver, line))
        except MathError as e:
            print(e.to_json(indent=2), file=sys.stderr)

if __name__ == "__main__":
    sys.exit(main())