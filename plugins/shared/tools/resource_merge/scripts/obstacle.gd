extends CharacterBody2D
## 障碍：匀速下落 + 离开屏幕后自动释放。
## 节点结构：CharacterBody2D / CollisionShape2D / ColorRect。
## 物理层：collision_layer=2(obstacle) / mask=1(只与玩家产生 body_entered 信号)。
## 速度由 spawner 在生成时写入 fall_speed（难度递增由 GameManager.difficulty 控制）。

var fall_speed: float = 220.0


func _physics_process(delta: float) -> void:
	velocity = Vector2(0.0, fall_speed)
	move_and_slide()

	# 超出屏幕底端即释放，避免节点堆积。
	if position.y > get_viewport_rect().size.y + 64.0:
		queue_free()


func _ready() -> void:
	# 加入 obstacle 组供玩家 Area2D 识别。
	add_to_group("obstacle")
