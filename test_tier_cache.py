"""测试 tier 缓存清除机制。"""
import sys
sys.path.insert(0, "src")

from pipeline.plugin_resolver import _tier_cache, resolve_tier, apply_agent_model_override
from config.models import get_model_config_loader

def test_tier_cache_clear():
    """测试 _tier_cache 在 apply_agent_model_override 中被清除。"""
    print("=== 测试 tier 缓存清除 ===")

    # 模拟旧缓存
    _tier_cache["large"] = "minimax-m3"
    _tier_cache["small"] = "minimax-m3"
    print(f"1. 模拟旧缓存: {_tier_cache}")

    # 获取 loader
    loader = get_model_config_loader()
    services = {"model_loader": loader}

    # 模拟 agent 配置
    class MockAgent:
        model_name = None
        model_tier = "large"
        config_id = "test_agent"

    # 调用 apply_agent_model_override（会清除 _tier_cache）
    print("2. 调用 apply_agent_model_override...")
    apply_agent_model_override(None, MockAgent(), services)

    # 验证缓存已清除
    print(f"3. 调用后缓存: {_tier_cache}")
    if "large" not in _tier_cache and "small" not in _tier_cache:
        print("✓ 缓存已清除")
    else:
        print("✗ 缓存未清除")
        return False

    # 验证 resolve_tier 返回新值
    result = resolve_tier("large", services)
    print(f"4. resolve_tier('large') = {result}")
    if result == "minimax-m3-apigo":
        print("✓ 返回正确的模型 ID")
    else:
        print(f"✗ 返回错误的模型 ID: {result}")
        return False

    return True

if __name__ == "__main__":
    success = test_tier_cache_clear()
    sys.exit(0 if success else 1)
