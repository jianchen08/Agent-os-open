"""基于 Flask 的简单 REST API 服务，提供用户 CRUD 接口，使用内存列表存储数据。"""

from __future__ import annotations

from flask import Flask, jsonify, request

app = Flask(__name__)

# 内存数据存储
users: list[dict[str, str | int]] = []
_next_id: int = 1


@app.route("/users", methods=["GET"])
def get_users() -> tuple:
    """获取所有用户列表。"""
    return jsonify(users), 200


@app.route("/users/<int:user_id>", methods=["GET"])
def get_user(user_id: int) -> tuple:
    """根据 ID 获取单个用户。"""
    user = next((u for u in users if u["id"] == user_id), None)
    if user is None:
        return jsonify({"error": "User not found"}), 404
    return jsonify(user), 200


@app.route("/users", methods=["POST"])
def create_user() -> tuple:
    """创建新用户。"""
    global _next_id
    data = request.get_json(silent=True)
    if not data or "name" not in data:
        return jsonify({"error": "Missing 'name' field"}), 400

    user = {"id": _next_id, "name": data["name"]}
    _next_id += 1
    users.append(user)
    return jsonify(user), 201


@app.route("/users/<int:user_id>", methods=["PUT"])
def update_user(user_id: int) -> tuple:
    """更新指定用户信息。"""
    user = next((u for u in users if u["id"] == user_id), None)
    if user is None:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True)
    if not data or "name" not in data:
        return jsonify({"error": "Missing 'name' field"}), 400

    user["name"] = data["name"]
    return jsonify(user), 200


@app.route("/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id: int) -> tuple:
    """删除指定用户。"""
    global users
    user = next((u for u in users if u["id"] == user_id), None)
    if user is None:
        return jsonify({"error": "User not found"}), 404

    users = [u for u in users if u["id"] != user_id]
    return jsonify({"message": "User deleted"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
