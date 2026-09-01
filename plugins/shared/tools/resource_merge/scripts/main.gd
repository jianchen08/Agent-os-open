extends Node2D
## 主场景入口：每物理帧把 delta 推给 GameManager 推进计时。

func _physics_process(delta: float) -> void:
	GameManager.tick(delta)