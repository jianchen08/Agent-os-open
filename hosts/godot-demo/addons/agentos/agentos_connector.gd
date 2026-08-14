## AgentOS 宿主接入插件（Godot 4 EditorPlugin）
##
## 作为灵汐 game_engine 连接器
## (plugins/shared/system/connectors/creative/game_engine.py) 的"对端"：
## 在 Godot 4 编辑器内启动一个极简 TCP/HTTP 服务，监听 127.0.0.1:9600。
##
## 端口 9600 与 game_engine.py 的默认 endpoint (http://127.0.0.1:9600) 对齐。
##
## 支持的端点（最小实现）：
##   GET  /health      -> 200 { status:"ok", version:"..." }
##   GET  /status      -> 200 { version, engine:"godot", engine_version, project }
##   GET  /context     -> 200 { active_scene, selected_object, scene_name, engine_version, selected_objects }
##   GET  /scene       -> 200 { name, path, node_count, root }
##   GET  /selection   -> 200 { selected: [...] }
##   GET  /capabilities-> 200 { capabilities:[...] }
##   POST /execute     -> 200 { success, command, args }   (最小：仅回显，不真正执行任意命令)
##
## 说明：Godot 没有 HTTP server 类，这里基于 TCPServer 手写一个最简 HTTP/1.x 解析，
## 只解析 method + path + query + body，返回固定 JSON。足以支撑灵汐连接器探活与取上下文。
@tool
extends EditorPlugin

const DEFAULT_PORT := 9600
const DEFAULT_HOST := "127.0.0.1"
const PLUGIN_VERSION := "0.2.0"
const MAX_HEADER_BYTES := 16 * 1024  # 单个请求头部最多读取 16KB，防止异常连接占用内存

var _server: TCPServer = null
var _clients: Array = []  # 正在处理的 StreamPeerTCP 连接

## 在插件项目设置中可覆盖的端口（Project Settings -> AgentOS/Host/Port）。
func _get_port() -> int:
	var port: int = DEFAULT_PORT
	if ProjectSettings.has_setting("agentos/host/port"):
		var v = ProjectSettings.get_setting("agentos/host/port")
		if v is int:
			port = v
	return port

func _get_host() -> String:
	var host: String = DEFAULT_HOST
	if ProjectSettings.has_setting("agentos/host/address"):
		var v = ProjectSettings.get_setting("agentos/host/address")
		if v is String and v != "":
			host = v
	return host

func _enter_tree() -> void:
	_start_server()

func _exit_tree() -> void:
	_stop_server()

func _start_server() -> void:
	if _server != null:
		return
	_server = TCPServer.new()
	var port := _get_port()
	var host := _get_host()
	var err := _server.listen(port, host)
	if err != OK:
		printerr("[AgentOS] TCPServer.listen(%s:%d) 失败，错误码: %d" % [host, port, err])
		_server = null
		return
	print("[AgentOS] 宿主服务已启动: http://%s:%d" % [host, port])

func _stop_server() -> void:
	for c in _clients:
		if c is StreamPeerTCP:
			(c as StreamPeerTCP).disconnect_from_host()
	_clients.clear()
	if _server != null:
		_server.stop()
		_server = null
		print("[AgentOS] 宿主服务已停止")

## 每帧轮询：接收新连接、读取并响应每个连接的请求。
func _process(_delta: float) -> void:
	if _server == null:
		return

	# 接受新连接
	while _server.is_connection_available():
		var conn: StreamPeerTCP = _server.take_connection()
		if conn != null:
			conn.set_big_endian(false)
			_clients.append(conn)

	# 处理已有连接
	var still_alive: Array = []
	for c in _clients:
		if not (c is StreamPeerTCP):
			continue
		var conn: StreamPeerTCP = c as StreamPeerTCP
		conn.poll()
		var status := conn.get_status()
		if status == StreamPeerTCP.STATUS_NONE or status == StreamPeerTCP.STATUS_ERROR:
			conn.disconnect_from_host()
			continue
		if status != StreamPeerTCP.STATUS_CONNECTED:
			still_alive.append(conn)
			continue

		# 尝试读取并处理一个请求
		if _try_handle(conn):
			# 处理完一个请求后断开（HTTP/1.0 风格，无 keep-alive）
			conn.disconnect_from_host()
		else:
			still_alive.append(conn)
	_clients = still_alive

## 尝试读取一个完整 HTTP 请求并响应；返回 true 表示已响应（连接应关闭）。
func _try_handle(conn: StreamPeerTCP) -> bool:
	var avail := conn.get_available_bytes()
	if avail <= 0:
		return false

	# 读取至多 MAX_HEADER_BYTES 的原始数据（Godot 4.4+ 起 StreamPeer.get_partial 更名为 get_partial_data，
	# 返回 [错误码, 数据] 两元素数组）
	var got := conn.get_partial_data(mini(avail, MAX_HEADER_BYTES))
	if got.size() < 2 or not got[1] is PackedByteArray:
		return false
	var raw: PackedByteArray = got[1]
	if raw.size() == 0:
		return false
	var text: String = raw.get_string_from_utf8()

	# 必须读到头部结束符 \r\n\r\n 才能处理；否则等待更多数据
	var header_end: int = text.find("\r\n\r\n")
	if header_end == -1:
		return false

	var header_part: String = text.substr(0, header_end)
	var body: String = text.substr(header_end + 4)

	var lines: PackedStringArray = header_part.split("\r\n")
	if lines.size() == 0:
		_respond(conn, 400, {"error": "bad request"})
		return true

	# 解析请求行：METHOD PATH HTTP/1.1
	var request_line: String = lines[0]
	var parts: PackedStringArray = request_line.split(" ")
	if parts.size() < 2:
		_respond(conn, 400, {"error": "bad request line"})
		return true
	var method: String = parts[0]
	var raw_target: String = parts[1]
	var path := raw_target
	var query := ""
	var q_idx: int = raw_target.find("?")
	if q_idx != -1:
		path = raw_target.substr(0, q_idx)
		query = raw_target.substr(q_idx + 1)

	var result := _route(method, path, query, body)
	_respond(conn, 200, result)
	return true

## 路由分发，返回要序列化为 JSON 响应体的字典。
func _route(method: String, path: String, _query: String, body: String) -> Dictionary:
	match path:
		"/health":
			return {"status": "ok", "version": PLUGIN_VERSION}
		"/status":
			return _build_status()
		"/context":
			return _build_context()
		"/scene":
			return _build_scene_info()
		"/selection":
			return _build_selection()
		"/capabilities":
			return {"capabilities": _capabilities()}
		"/execute":
			return _handle_execute(body)
		# 以下端点返回占位 OK，保持与连接器约定接口形状兼容
		"/screenshot":
			return {"url": "", "path": "", "note": "screenshot not supported in minimal demo"}
		"/assets", "/assets/import":
			return {"assets": [], "note": "assets endpoint stub"}
		"/play":
			return {"success": true, "note": "EditorPlugin does not control play in minimal demo"}
		"/stop":
			return {"success": true,	"note": "EditorPlugin does not control stop in minimal demo"}
		_:
			return {"error": "unknown endpoint: %s %s" % [method, path]}

## 构建 /status 响应。对齐 game_engine.connect() 读取的 version 字段。
func _build_status() -> Dictionary:
	return {
		"status": "ok",
		"version": PLUGIN_VERSION,
		"engine": "godot",
		"engine_version": Engine.get_version_info().get("string", "unknown"),
		"project": _project_name(),
	}

## 构建 /context 响应，字段对齐 game_engine.get_context() 期望的字段。
func _build_context() -> Dictionary:
	var scene := _current_scene_info()
	var sel := _editor_selection_names()
	return {
		"active_scene": scene.get("path", ""),
		"selected_object": sel[0] if sel.size() > 0 else "",
		"scene_name": scene.get("name", ""),
		"engine_version": Engine.get_version_info().get("string", "unknown"),
		"selected_objects": sel,
	}

## 构建 /scene 响应。
func _build_scene_info() -> Dictionary:
	return _current_scene_info()

## 构建 /selection 响应。
func _build_selection() -> Dictionary:
	return {"selected": _editor_selection_names()}

## 最小执行：不真正执行任意命令（安全），仅回显收到的命令与参数。
func _handle_execute(body: String) -> Dictionary:
	var parsed: Dictionary = {}
	if body != "":
		var json := JSON.new()
		if json.parse(body) == OK and json.data is Dictionary:
			parsed = json.data
	return {
		"success": true,
		"command": parsed.get("command", ""),
		"args": parsed.get("args", {}),
		"note": "minimal demo: command echoed, not executed",
	}

## 能力列表，对齐 game_engine ConnectorInfo.capabilities 的子集。
func _capabilities() -> PackedStringArray:
	return PackedStringArray([
		"capture_screenshot",
		"get_scene_info",
		"list_assets",
		"import_asset",
		"execute_command",
		"get_selection",
		"play_preview",
		"stop_preview",
	])

## 取当前编辑的场景信息（路径 / 名称 / 根节点 / 节点数）。
func _current_scene_info() -> Dictionary:
	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	var edited_scene_root: Node = null
	if edt != null:
		edited_scene_root = edt.get_edited_scene_root()

	if edited_scene_root == null:
		return {"name": "", "path": "", "root": "", "node_count": 0}

	var scene_path := edited_scene_root.scene_file_path
	return {
		"name": edited_scene_root.name,
		"path": scene_path,
		"root": edited_scene_root.get_class(),
		"node_count": _count_nodes(edited_scene_root),
	}

## 取编辑器当前选中节点的名称列表。
func _editor_selection_names() -> PackedStringArray:
	var names := PackedStringArray()
	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	if edt == null:
		return names
	var esel := edt.get_selection()
	if esel == null:
		return names
	var sel_nodes := esel.get_selected_nodes()
	for n in sel_nodes:
		if n is Node:
			names.append((n as Node).name)
	return names

func _count_nodes(root: Node) -> int:
	var count := 0
	var stack: Array = [root]
	while stack.size() > 0:
		var n: Node = stack.pop_back()
		count += 1
		for c in n.get_children():
			stack.append(c)
	return count

func _project_name() -> String:
	var cfg := ConfigFile.new()
	if cfg.load("res://project.godot") == OK:
		var nm = cfg.get_value("application", "config/name", "")
		if nm != "":
			return String(nm)
	return ""

## 写回一个 HTTP/1.0 响应，body 为 JSON 字符串。
func _respond(conn: StreamPeerTCP, status: int, payload: Variant) -> void:
	var json_text := JSON.stringify(payload)
	var status_text := "OK" if status == 200 else "Bad Request" if status == 400 else "Internal Server Error"
	var header := "HTTP/1.0 %d %s\r\n" % [status, status_text]
	header += "Content-Type: application/json; charset=utf-8\r\n"
	header += "Access-Control-Allow-Origin: *\r\n"
	header += "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
	header += "Access-Control-Allow-Headers: Content-Type\r\n"
	header += "Content-Length: %d\r\n" % json_text.length()
	header += "Connection: close\r\n"
	header += "\r\n"
	conn.put_data((header + json_text).to_utf8_buffer())
