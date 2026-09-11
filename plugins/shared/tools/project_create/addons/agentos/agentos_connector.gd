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

# AgentOS 插件推送端点（内核默认端口 9100；可在 Project Settings agentos/push_endpoint 覆盖）
const DEFAULT_PUSH_ENDPOINT := "http://127.0.0.1:9100/ext/pipeline_godot_context/selection"
const PUSH_DEBOUNCE_SEC := 0.3
const HEARTBEAT_INTERVAL_SEC := 5.0

var _server: TCPServer = null
var _clients: Array = []  # 正在处理的 StreamPeerTCP 连接

# ── 选中推送（Godot → AgentOS，事件驱动，无轮询）──
var _push_requests: Array = []  # HTTPRequest 节点池
var _push_rr := 0
var _debounce_timer: Timer = null
var _heartbeat_timer: Timer = null

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
	_start_push()

func _exit_tree() -> void:
	_stop_push()
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

## ── 选中推送：EditorSelection.selection_changed → POST 到 AgentOS 插件端点 ──

func _get_push_endpoint() -> String:
	if ProjectSettings.has_setting("agentos/push_endpoint"):
		var v = ProjectSettings.get_setting("agentos/push_endpoint")
		if v is String and v != "":
			return v
	return DEFAULT_PUSH_ENDPOINT

func _start_push() -> void:
	for i in 2:
		var req := HTTPRequest.new()
		req.name = "AgentOSPush%d" % i
		req.timeout = 2.0
		add_child(req)
		_push_requests.append(req)
	_debounce_timer = Timer.new()
	_debounce_timer.name = "AgentOSPushDebounce"
	_debounce_timer.one_shot = true
	_debounce_timer.wait_time = PUSH_DEBOUNCE_SEC
	_debounce_timer.timeout.connect(_push_selection_now)
	add_child(_debounce_timer)
	_heartbeat_timer = Timer.new()
	_heartbeat_timer.name = "AgentOSPushHeartbeat"
	_heartbeat_timer.wait_time = HEARTBEAT_INTERVAL_SEC
	_heartbeat_timer.timeout.connect(_push_heartbeat)
	add_child(_heartbeat_timer)
	_heartbeat_timer.start()

	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	if edt != null:
		var esel := edt.get_selection()
		if esel != null:
			esel.selection_changed.connect(_on_selection_changed)
		var dock := edt.get_file_system_dock()
		if dock != null:
			dock.selection_changed.connect(_on_selection_changed)
	# 插件启用即推一次初始快照（AgentOS 后启动也能拿到当前状态）
	_push("selection")

func _stop_push() -> void:
	_push("offline")
	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	if edt != null:
		var esel := edt.get_selection()
		if esel != null and esel.selection_changed.is_connected(_on_selection_changed):
			esel.selection_changed.disconnect(_on_selection_changed)
		var dock := edt.get_file_system_dock()
		if dock != null and dock.selection_changed.is_connected(_on_selection_changed):
			dock.selection_changed.disconnect(_on_selection_changed)
	if _heartbeat_timer != null:
		_heartbeat_timer.stop()
		_heartbeat_timer.queue_free()
		_heartbeat_timer = null
	if _debounce_timer != null:
		_debounce_timer.stop()
		_debounce_timer.queue_free()
		_debounce_timer = null
	for req in _push_requests:
		(req as HTTPRequest).queue_free()
	_push_requests.clear()

func _on_selection_changed() -> void:
	# 防抖：拖框/多选过程高频触发，只推最后一次
	_debounce_timer.start()

func _push_selection_now() -> void:
	_push("selection")

func _push_heartbeat() -> void:
	_push("heartbeat")

func _push(kind: String) -> void:
	if _push_requests.is_empty():
		return
	var req: HTTPRequest = _push_requests[_push_rr % _push_requests.size()]
	_push_rr += 1
	if req.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
		return  # 池内忙则跳过，下个心跳/信号补推
	var payload := JSON.stringify(_build_push_payload(kind))
	var headers := PackedStringArray(["Content-Type: application/json"])
	var err := req.request(_get_push_endpoint(), headers, HTTPClient.METHOD_POST, payload)
	if err != OK:
		return  # AgentOS 未启动等情况，静默失败

func _build_push_payload(kind: String) -> Dictionary:
	var items := _editor_selection_details()
	# 场景节点未选中时回退文件系统 Dock 的选中（selection_changed 即时，心跳兜底）
	if items.is_empty():
		items = _file_selection_details()
	var sig_parts := PackedStringArray()
	for it in items:
		sig_parts.append("%s@%s" % [it.get("name", ""), it.get("path", "")])
	var scene := _current_scene_info()
	return {
		"type": kind,
		"engine": "godot",
		"engine_version": Engine.get_version_info().get("string", "unknown"),
		"project": _project_name(),
		"scene": scene,
		"items": items,
		"signature": ";".join(sig_parts),
		"ts": int(Time.get_unix_time_from_system() * 1000),
	}

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

	if path == "/selection/preview":
		_respond_preview(conn, query)
		return true
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
		"selection_detail": _editor_selection_details(),
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

## 选中节点详情列表（推送 payload 与 /context.selection_detail 共用）。
func _editor_selection_details() -> Array:
	var out: Array = []
	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	if edt == null:
		return out
	var esel := edt.get_selection()
	if esel == null:
		return out
	for n in esel.get_selected_nodes():
		if not (n is Node):
			continue
		var node := n as Node
		out.append({
			"name": String(node.name),
			"type": node.get_class(),
			"path": String(node.get_path()),
			"position": _node_position(node),
			"preview_kind": _preview_kind_for(node),
		})
	return out

## 位置描述：2D/3D 节点取全局坐标，其余类型无位置返回空串。
func _node_position(node: Node) -> String:
	if node is Node2D:
		return str((node as Node2D).global_position)
	if node is Node3D:
		return str((node as Node3D).global_position)
	return ""

## 文件系统 Dock 的选中文件/目录（场景节点未选中时作为引用回退；
## selection_changed 即时推送，心跳保留为兜底周期）。
## get_selected_paths 在无实际选中时回退返回树根 "res://"（Godot issue #88228；
## 4.7.1 实证导航后仍返回 ["res://"]）——"res://" 是「什么都没选」的哨兵值而非
## 引用，必须过滤；真实选中的文件与子目录照常进引用。
func _file_selection_details() -> Array:
	var out: Array = []
	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	if edt == null or not edt.has_method("get_selected_paths"):
		return out
	for p in edt.get_selected_paths():
		var s := String(p)
		var t := s.trim_suffix("/")  # 目录选中带尾斜杠，name 须取自去斜杠路径
		if s == "res://" or t.is_empty() or t == "res://":
			continue
		out.append({
			"name": t.get_file(),
			"type": "file" if FileAccess.file_exists(s) else "directory",
			"path": s,
		})
	return out

## 2D 视口左键点击 = 显式选择动作：同节点重复点击也触发推送
## （Godot 对已选中节点不重发 selection_changed，动作语义靠本钩子补齐）。
func _forward_canvas_gui_input(event: InputEvent) -> bool:
	if event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT and event.pressed:
		if _debounce_timer != null:
			_debounce_timer.start()
	return false

## 预览方式：带贴图的节点直接用贴图缩略图；其余用编辑器视口截图。
func _preview_kind_for(node: Node) -> String:
	var tex = node.get("texture")
	if tex != null and tex is Texture2D:
		return "texture"
	return "viewport"

## /selection/preview 的 PNG 字节（最长边 ≤512px）；无预览返回空。
func _preview_png(index: int) -> PackedByteArray:
	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	if edt == null:
		return PackedByteArray()
	var esel := edt.get_selection()
	if esel == null:
		return PackedByteArray()
	var nodes := esel.get_selected_nodes()
	if index < 0 or index >= nodes.size():
		return PackedByteArray()
	var node := nodes[index] as Node
	var img: Image = null
	if _preview_kind_for(node) == "texture":
		var tex := node.get("texture") as Texture2D
		if tex != null:
			img = tex.get_image()
	else:
		img = _capture_editor_viewport(node is Node3D)
	if img == null or img.is_empty():
		return PackedByteArray()
	_resize_image_max(img, 512)
	return img.save_png_to_buffer()

## 保持纵横比缩放到最长边 ≤ max_px。
func _resize_image_max(img: Image, max_px: int) -> void:
	var w := img.get_width()
	var h := img.get_height()
	if w <= max_px and h <= max_px:
		return
	var scale := float(max_px) / float(maxi(w, h))
	img.resize(int(w * scale), int(h * scale), Image.INTERPOLATE_LANCZOS)

## 截取编辑器 2D/3D 视口（API 缺失或无渲染环境返回 null）。
func _capture_editor_viewport(is_3d: bool) -> Image:
	var edt: EditorInterface = get_editor_interface() if has_method("get_editor_interface") else null
	if edt == null:
		return null
	var vp = null
	if is_3d:
		if edt.has_method("get_editor_viewport_3d"):
			vp = edt.get_editor_viewport_3d()
	elif edt.has_method("get_editor_viewport_2d"):
		vp = edt.get_editor_viewport_2d()
	if vp == null:
		return null
	var tex = vp.get_texture()
	if tex == null:
		return null
	return tex.get_image()

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

## /selection/preview?index=N：贴图/视口截图 PNG；无预览返回 404 JSON。
func _respond_preview(conn: StreamPeerTCP, query: String) -> void:
	var idx_at := query.find("index=")
	if idx_at == -1:
		_respond(conn, 404, {"error": "missing index"})
		return
	var idx := query.substr(idx_at + 6).to_int()
	var png := _preview_png(idx)
	if png.size() == 0:
		_respond(conn, 404, {"error": "no preview for index %d" % idx})
		return
	var header := "HTTP/1.0 200 OK\r\n"
	header += "Content-Type: image/png\r\n"
	header += "Access-Control-Allow-Origin: *\r\n"
	header += "Content-Length: %d\r\n" % png.size()
	header += "Connection: close\r\n"
	header += "\r\n"
	conn.put_data(header.to_utf8_buffer())
	conn.put_data(png)

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
