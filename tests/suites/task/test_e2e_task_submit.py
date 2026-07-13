# -*- coding: utf-8 -*-
"""E2E测试执行器：基于 e2e_task_submit.yaml 配置执行任务提交测试（轻量版）"""
import os
import sys
import yaml
import pytest
from pathlib import Path

# 设置路径
sys.path.insert(0, str(Path(__file__).parent / "src"))
os.environ["PYTHONPATH"] = "src"


@pytest.mark.integration
class TestE2ETaskSubmit:
    """任务提交E2E测试类"""

    @pytest.fixture(scope="class")
    def config_path(self):
        """配置文件路径 - 文件位于 tests/ 根目录"""
        return Path(__file__).resolve().parent.parent.parent / "e2e_task_submit.yaml"

    @pytest.fixture(scope="class")
    def test_config(self, config_path):
        """加载测试配置"""
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def test_config_file_exists(self, config_path):
        """测试1: 验证测试配置文件存在"""
        assert config_path.exists(), f"测试配置文件不存在: {config_path}"
        print(f"[PASS] 配置文件存在: {config_path}")

    def test_config_structure(self, test_config):
        """测试2: 验证配置文件结构正确"""
        # 检查顶层必需字段
        assert 'name' in test_config, "缺少 name 字段"
        assert 'description' in test_config, "缺少 description 字段"
        assert 'test_cases' in test_config, "缺少 test_cases 字段"

        # 检查环境配置
        if 'environment' in test_config:
            env = test_config['environment']
            assert 'streaming' in env, "environment 缺少 streaming 配置"
            assert 'auto_approve' in env, "environment 缺少 auto_approve 配置"

        print(f"[PASS] 配置结构正确: name={test_config['name']}")

    def test_test_cases_not_empty(self, test_config):
        """测试3: 验证测试用例非空"""
        test_cases = test_config.get('test_cases', [])
        assert len(test_cases) > 0, "测试用例列表为空"
        print(f"[PASS] 测试用例数量: {len(test_cases)}")

    def test_normal_submit_coverage(self, test_config):
        """测试4: 验证覆盖正常任务提交场景"""
        test_cases = test_config.get('test_cases', [])

        # 查找正常提交场景
        normal_submit = None
        for tc in test_cases:
            if tc.get('case_id') == 'TC001_normal_submit':
                normal_submit = tc
                break

        assert normal_submit is not None, "缺少 TC001_normal_submit 正常提交测试"

        # 验证场景内容
        assert 'input' in normal_submit, "正常提交场景缺少 input"
        assert 'prompt' in normal_submit['input'], "缺少 prompt"
        assert 'task_submit' in normal_submit['input']['prompt'].lower(), "prompt 未包含 task_submit"

        print(f"[PASS] 覆盖正常提交场景: {normal_submit['name']}")

    def test_input_validation_coverage(self, test_config):
        """测试5: 验证覆盖输入验证场景"""
        test_cases = test_config.get('test_cases', [])

        # 查找输入验证场景
        validation_cases = [
            tc for tc in test_cases
            if 'missing' in tc.get('case_id', '').lower() or 'invalid' in tc.get('case_id', '').lower()
        ]

        assert len(validation_cases) >= 3, f"输入验证场景不足，需要至少3个，实际 {len(validation_cases)}"

        # 验证包含各类错误场景
        case_ids = [tc['case_id'] for tc in validation_cases]
        has_required = any('missing' in cid.lower() for cid in case_ids)
        has_type = any('target_type' in cid.lower() for cid in case_ids)
        has_priority = any('priority' in cid.lower() for cid in case_ids)

        assert has_required, "缺少必填字段验证场景"
        assert has_type, "缺少目标类型验证场景"
        assert has_priority, "缺少优先级验证场景"

        print(f"[PASS] 覆盖输入验证场景: {len(validation_cases)} 个")

    def test_error_handling_coverage(self, test_config):
        """测试6: 验证覆盖错误处理场景"""
        test_cases = test_config.get('test_cases', [])

        error_handling_cases = [
            tc for tc in test_cases
            if tc.get('expected', {}).get('error_handled') == True
        ]

        assert len(error_handling_cases) > 0, "缺少错误处理测试场景"

        # 验证错误处理验证点
        for case in error_handling_cases:
            assert 'validation' in case, f"错误处理场景 {case.get('case_id')} 缺少 validation"
            has_error_check = any(
                v.get('check') == 'error_handling'
                for v in case['validation']
            )
            assert has_error_check, f"场景 {case.get('case_id')} 缺少 error_handling 检查"

        print(f"[PASS] 覆盖错误处理场景: {len(error_handling_cases)} 个")

    def test_persistence_validation(self, test_config):
        """测试7: 验证任务持久化验证"""
        test_cases = test_config.get('test_cases', [])

        # 查找持久化测试场景
        persistence_cases = [
            tc for tc in test_cases
            if any(
                v.get('check') in ['task_persisted', 'data_file']
                for v in tc.get('validation', [])
            )
        ]

        assert len(persistence_cases) > 0, "缺少任务持久化验证场景"

        # 验证数据文件检查
        for case in persistence_cases:
            for v in case['validation']:
                if v.get('check') in ['task_persisted', 'data_file']:
                    if v.get('check') == 'data_file':
                        assert 'file' in v, "data_file 检查缺少 file 字段"
                    if v.get('check') == 'task_persisted':
                        assert 'data_file' in v, "task_persisted 检查缺少 data_file 字段"

        print(f"[PASS] 覆盖持久化验证: {len(persistence_cases)} 个场景")

    def test_success_feedback_coverage(self, test_config):
        """测试8: 验证提交成功反馈测试"""
        test_cases = test_config.get('test_cases', [])

        # 查找成功反馈场景
        success_case = None
        for tc in test_cases:
            if tc.get('case_id') == 'TC006_success_feedback':
                success_case = tc
                break

        assert success_case is not None, "缺少 TC006_success_feedback 成功反馈测试"

        # 验证成功场景的验证点
        expected = success_case.get('expected', {})
        assert expected.get('success') == True, "成功反馈场景期望 success=True"
        assert 'result_contains' in expected, "缺少 result_contains 验证"
        assert 'task_id' in expected['result_contains'], "缺少 task_id 字段验证"
        assert 'success' in expected['result_contains'], "缺少 success 字段验证"

        print(f"[PASS] 覆盖成功反馈验证")

    def test_acceptance_criteria_complete(self, test_config):
        """测试9: 验证验收标准完整"""
        acceptance = test_config.get('acceptance_criteria', [])

        required_criteria = [
            "测试文件存在且格式正确",
            "测试覆盖正常提交流程",
            "测试覆盖输入验证场景",
            "测试覆盖错误处理场景",
            "测试验证任务持久化",
        ]

        # 检查每个必需标准是否被提及
        ' '.join(acceptance)
        for criterion in required_criteria:
            assert any(criterion in c for c in acceptance), f"缺少验收标准: {criterion}"

        print(f"[PASS] 验收标准完整: {len(acceptance)} 项")

    def test_yaml_syntax_valid(self, config_path):
        """测试10: 验证YAML语法有效"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                yaml.safe_load(f)
            print(f"[PASS] YAML 语法有效")
        except yaml.YAMLError as e:
            pytest.fail(f"YAML 语法错误: {e}")


@pytest.mark.integration
class TestE2ETaskSubmitSemantic:
    """语义级验收测试"""

    @pytest.fixture(scope="class")
    def config_path(self):
        return Path(__file__).resolve().parent.parent.parent / "e2e_task_submit.yaml"

    @pytest.fixture(scope="class")
    def test_config(self, config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def test_full_coverage_statement(self, test_config):
        """验收：测试覆盖任务提交的完整流程"""
        test_cases = test_config.get('test_cases', [])

        # 验收标准检查
        coverage = {
            'normal_submit': False,      # 正常提交
            'validation': False,         # 输入验证
            'error_handling': False,     # 错误处理
            'success_feedback': False,   # 成功反馈
            'persistence': False,        # 持久化
        }

        for tc in test_cases:
            case_id = tc.get('case_id', '')

            if case_id == 'TC001_normal_submit':
                coverage['normal_submit'] = True
            if case_id == 'TC006_success_feedback':
                coverage['success_feedback'] = True
            if 'missing' in case_id.lower() or 'invalid' in case_id.lower():
                coverage['validation'] = True
            if tc.get('expected', {}).get('error_handled'):
                coverage['error_handling'] = True
            if any(v.get('check') in ['task_persisted', 'data_file'] for v in tc.get('validation', [])):
                coverage['persistence'] = True

        missing = [k for k, v in coverage.items() if not v]
        assert len(missing) == 0, f"测试覆盖不完整，缺少: {missing}"

        print(f"[PASS] 完整流程覆盖验证通过")
        print(f"  - 正常提交: {'✓' if coverage['normal_submit'] else '✗'}")
        print(f"  - 输入验证: {'✓' if coverage['validation'] else '✗'}")
        print(f"  - 错误处理: {'✓' if coverage['error_handling'] else '✗'}")
        print(f"  - 成功反馈: {'✓' if coverage['success_feedback'] else '✗'}")
        print(f"  - 持久化验证: {'✓' if coverage['persistence'] else '✗'}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
