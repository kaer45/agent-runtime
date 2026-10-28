from tasks.validator import ArgumentValidationError, ToolArgumentValidator
from tools.builtin import get_weather
from tools.registry import ToolRegistry

setup_registry = ToolRegistry()
setup_registry.register(get_weather)


def check(label: str, arguments: dict) -> None:
    validator = ToolArgumentValidator(setup_registry)
    try:
        validator.validate_arguments("get_weather", arguments)
    except ArgumentValidationError as exc:
        print(f"[FAIL] {label}: {exc}")
    else:
        print(f"[OK] {label}")


def main() -> None:
    check("正常参数", {"city": "成都"})
    check("缺少必填", {})
    check("类型错误", {"city": 123})
    check("多余参数", {"city": "成都", "unit": "celsius"})


if __name__ == "__main__":
    main()
