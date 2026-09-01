extends Node
## Autoload 单例：跨场景持久化游戏状态（存活秒数 / 难度等级 / 失败信号）。
## 由 project.godot 的 [autoload] 注册，名字 GameManager 在场景脚本中直接使用。

signal game_over(final_time: float)

## 当前存活秒数（HUD 显示来源）。
var elapsed: float = 0.0

## 难度等级（1~4），由 spawner 读取决定间隔/速度。
var difficulty: int = 1

## 是否处于游戏结束态（HUD 据此切换到失败面板）。
var is_game_over: bool = false


func _ready() -> void:
	reset()


## 重置所有状态，供 main.gd 在重试时调用。
func reset() -> void:
	elapsed = 0.0
	difficulty = 1
	is_game_over = false


## 每物理帧推进存活计时（main.gd 在 _physics_process 中调用）。
func tick(delta: float) -> void:
	if is_game_over:
		return
	elapsed += delta


## 触发游戏结束：冻结计时、广播最终分。
func trigger_game_over() -> void:
	if is_game_over:
		return
	is_game_over = true
	game_over.emit(elapsed)