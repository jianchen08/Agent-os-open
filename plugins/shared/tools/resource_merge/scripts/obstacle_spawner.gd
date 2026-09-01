extends Node2D
## 障碍生成器：Timer 周期性 instantiate 障碍模板，随机 X、阶梯递增速度/间隔。
## 节点结构：Node2D / Timer。
## 难度曲线：阶梯式（每 10 秒升 1 档），共 4 档；上限速度与下限间隔硬封顶。

const OBSTACLE_SCENE: PackedScene = preload("res://scenes/obstacle.tscn")
const DIFFICULTY_INTERVAL: float = 10.0  # 每 10 秒升一档
const SPEED_PER_TIER: float = 60.0        # 每档速度 +60
const BASE_SPEED: float = 220.0           # 1 档起始速度
const BASE_INTERVAL: float = 1.0          # 1 档生成间隔
const MIN_INTERVAL: float = 0.35          # 间隔下限（避免刷屏）

@export var viewport_width: float = 480.0


func _ready() -> void:
	_restart_timer_with(GameManager.difficulty)


func _process(_delta: float) -> void:
	# 难度阶梯：根据存活秒数计算当前档位（1~4）。
	var target_tier: int = clamp(1 + int(GameManager.elapsed / DIFFICULTY_INTERVAL), 1, 4)
	if target_tier != GameManager.difficulty:
		GameManager.difficulty = target_tier
		_restart_timer_with(target_tier)


func _restart_timer_with(tier: int) -> void:
	# 阶梯难度：tier 越大，生成间隔越小（封顶 MIN_INTERVAL）。
	var wait: float = max(BASE_INTERVAL - 0.18 * (tier - 1), MIN_INTERVAL)
	$Timer.wait_time = wait
	$Timer.start()


func _on_timer_timeout() -> void:
	var obs: CharacterBody2D = OBSTACLE_SCENE.instantiate()
	obs.position = Vector2(randf_range(24.0, viewport_width - 24.0), -32.0)
	# 阶梯速度：tier 越大，下落越快。
	obs.fall_speed = BASE_SPEED + SPEED_PER_TIER * (GameManager.difficulty - 1)
	add_child(obs)