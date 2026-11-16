extends CharacterBody2D
## 玩家：方向键移动 + Area2D 监听障碍进入。
## 节点结构：CharacterBody2D / CollisionShape2D / HitArea (Area2D) / ColorRect (Sprite 替代)。
## 物理层：collision_layer=1(player) / mask=0(主动移动，不需要撞障碍反弹)。

const SPEED: float = 320.0

@export var bounds_min: Vector2 = Vector2(24.0, 24.0)
@export var bounds_max: Vector2 = Vector2(456.0, 456.0)


func _physics_process(_delta: float) -> void:
	# 取方向键输入；Input.get_vector 已对角线归一化。
	var dir: Vector2 = Input.get_vector("move_left", "move_right", "move_up", "move_down")
	velocity = dir * SPEED
	move_and_slide()

	# 视口边界 clamp（防止方块跑出屏幕）。
	var vp: Vector2 = get_viewport_rect().size
	position.x = clamp(position.x, bounds_min.x, vp.x - bounds_min.x)
	position.y = clamp(position.y, bounds_min.y, vp.y - bounds_min.y)


func _on_hit_area_body_entered(body: Node2D) -> void:
	# 任何进入玩家 HitArea 且属于 obstacle 组的物体都判定为失败。
	if body.is_in_group("obstacle"):
		GameManager.trigger_game_over()
