from logging_config import setup_logging
from tools.builtin import brewing, get_current_time, get_weather,recommend_wine_pairing
from tools.catalog import ToolCatalog
from tools.registry import ToolRegistry

setup_logging()


def main() -> None:
    registry = ToolRegistry()
    registry.register(get_weather)
    registry.register(get_current_time)
    registry.register(brewing)
    registry.register(recommend_wine_pairing)

    catalog = ToolCatalog.from_registry(registry)

    print("=== 工具名 ===")
    print(catalog.names())

    print("\n=== 详细目录（Planner） ===")
    print(catalog.render())

    print("\n=== 紧凑目录（Router） ===")
    print(catalog.render_compact())

    print("\n=== 目录指纹 ===")
    print(catalog.fingerprint())


if __name__ == "__main__":
    main()
