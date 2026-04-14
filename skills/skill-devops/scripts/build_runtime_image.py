#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
构建 Agent Runtime 镜像

使用宿主机已安装的依赖，创建预装依赖的 Docker 镜像

使用方式:
    python scripts/build_runtime_image.py

效果:
    创建 agent-runtime:latest 镜像，包含所有 Python 依赖
    之后创建容器时无需再安装依赖
"""

import docker
import os
import sys


def build_runtime_image(image_name: str = "agent-runtime:latest"):
    """构建包含依赖的运行时镜像

    Args:
        image_name: 镜像名称
    """
    client = docker.from_env()

    print("=" * 60)
    print("Agent Runtime 镜像构建")
    print("=" * 60)

    # 1. 获取项目路径
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    requirements_file = os.path.join(project_root, "requirements.txt")

    if not os.path.exists(requirements_file):
        print(f"错误: requirements.txt 不存在: {requirements_file}")
        sys.exit(1)

    print(f"\n1. 项目路径: {project_root}")
    print(f"   requirements.txt: {requirements_file}")

    # 2. 创建临时容器
    print("\n2. 创建临时容器...")
    temp_container = client.containers.run(
        image="python:3.11-slim",
        detach=True,
        command="tail -f /dev/null",
        name="agent-runtime-builder-temp",
        remove=True,
    )
    print(f"   容器已创建: {temp_container.id}")

    try:
        # 3. 挂载 requirements.txt
        print("\n3. 挂载 requirements.txt...")
        # 停止并删除旧容器（如果存在）
        try:
            old = client.containers.get("agent-runtime-builder-temp")
            old.stop()
            old.remove()
        except:
            pass

        temp_container = client.containers.create(
            image="python:3.11-slim",
            detach=True,
            command="tail -f /dev/null",
            name="agent-runtime-builder-temp",
            volumes={requirements_file: {"bind": "/workspace/requirements.txt", "mode": "ro"}},
        )
        temp_container.start()
        print("   挂载完成")

        # 4. 安装依赖
        print("\n4. 安装依赖（这可能需要几分钟）...")
        import time
        start_time = time.time()

        exit_code, output = temp_container.exec_run(
            cmd="pip install -r /workspace/requirements.txt",
            stdout=True,
            stderr=True,
        )

        elapsed = time.time() - start_time
        print(f"   安装耗时: {elapsed:.1f}秒")

        if exit_code != 0:
            stderr_text = output[1].decode() if output[1] else ""
            stdout_text = output[0].decode() if output[0] else ""
            print(f"   安装失败!")
            print(f"   stdout: {stdout_text[:500]}")
            print(f"   stderr: {stderr_text[:500]}")
            sys.exit(1)

        print("   依赖安装完成")

        # 5. 提交镜像
        print("\n5. 提交镜像...")
        temp_container.stop()

        image = temp_container.commit(
            repository=image_name.split(":")[0],
            tag=image_name.split(":")[1] if ":" in image_name else "latest",
        )
        print(f"   镜像已创建: {image.id}")
        print(f"   镜像名称: {image_name}")

        # 6. 清理临时容器
        print("\n6. 清理...")
        try:
            temp_container.remove()
        except:
            pass

        print("\n" + "=" * 60)
        print(f"构建完成! 镜像: {image_name}")
        print(f"耗时: {elapsed:.1f}秒")
        print("=" * 60)
        print("\n使用方法:")
        print(f"  修改 config/isolation/isolation_config.yaml 中的 image 配置为: {image_name}")
        print("  或者修改 CuaProvider 的默认 image 参数")

    except Exception as e:
        print(f"\n错误: {e}")
        try:
            temp_container.remove(force=True)
        except:
            pass
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="构建 Agent Runtime 镜像")
    parser.add_argument(
        "--image",
        "-i",
        default="agent-runtime:latest",
        help="镜像名称 (默认: agent-runtime:latest)",
    )
    args = parser.parse_args()

    build_runtime_image(args.image)