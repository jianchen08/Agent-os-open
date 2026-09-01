extends CanvasLayer
## HUD：右上角存活秒数 + 失败时显示最终得分 + Retry 按钮。
## 节点结构：CanvasLayer / ColorRect (半透明失败面板) / Label (计时) / Label (失败) / Button (Retry)。

@onready var time_label: Label = $TimeLabel
@onready var game_over_panel: ColorRect = $GameOverPanel
@onready var final_label: Label = $GameOverPanel/VBox/FinalLabel
@onready var retry_button: Button = $GameOverPanel/VBox/RetryButton


func _process(_delta: float) -> void:
	# HUD 持续刷新存活秒数（保留 1 位小数）。
	time_label.text = "%.1fs" % GameManager.elapsed


func _ready() -> void:
	game_over_panel.visible = false
	GameManager.game_over.connect(_on_game_over)
	retry_button.pressed.connect(_on_retry_pressed)


func _on_game_over(final_time: float) -> void:
	game_over_panel.visible = true
	final_label.text = "Game Over\nFinal: %.1fs" % final_time


func _on_retry_pressed() -> void:
	# 重置单例状态并重载当前场景，等价于新一局。
	GameManager.reset()
	get_tree().reload_current_scene()