/*
 * 灵汐安卓壳层轮询脚本 —— 由 MainActivity 在页面加载完成后 evaluateJavascript 注入。
 *
 * 职责（零前端改动，与前端 bundle 无关，老 WebView 亦可执行）：
 * 1. 接 FGS 通知「停止」按钮事件（FGS 生命周期归原生 MainActivity onStart/onStop
 *    分流：前台不挂、退后台才挂，remote 模式恒不挂）；
 * 2. 30s 轮询「待审批批卡 + 任务终态」，增量转系统通知（@capacitor/local-notifications）；
 * 3. 去重已见集合存 localStorage（容量截断 200 条；首启只建快照不轰炸）；
 * 4. app 前台（document.visibilityState === 'visible'）只更新已见集合不发系统通知；
 * 5. 状态栏图标明暗随 app 主题（@capacitor/status-bar）：监听 <html> class 变化，
 *    html.dark → Style.DARK（浅色图标），无 dark → Style.LIGHT（深色图标）。
 *    只切图标 appearance，不触背景/overlay（v1.5 全屏沉浸基座不动）。
 *    @capacitor/status-bar@8 在包内（package.json），原生侧经 capacitor.plugins.json
 *    自动注册，JS 经 window.Capacitor.Plugins.StatusBar 调用。
 *
 * 取数契约（D12 后）：access token 只存主应用内存，壳读不到——每轮先用
 * localStorage 的 refresh token（按后端分键，tokenKeyFor：默认档裸键
 * refresh_token / 自定义后端 refresh_token@<base>，与前端 shellApiBase 同契约）
 * 调 /api/v1/auth/refresh 换短时 access token，仅存 JS 内存当轮使用、绝不落盘；
 * 服务端单次轮换返回的新 refresh_token 必须写回原键（否则主应用续期用已作废
 * 旧值被登出）。换发失败静默跳过本轮（console 留痕），绝不轰炸。
 *
 * 目标地址：localStorage.server_base_url（前端连接设置页写入，用户设置优先）
 * > 构建期注入基址（window.LINGXI_SHELL_CONFIG.apiBase，syncweb 生成 shell-config.js：
 *   云端包=云基址、本机包=127.0.0.1 回环），每轮现读——连接页改地址后轮询即时跟随，
 *   无需重启。壳层无注入兜底（本轮静默跳过），不再内置硬编码地址。
 *
 * 错误语义：无凭据 / 401 / 网络失败 / 解析失败一律静默跳过本轮（console 留痕），
 * 绝不 throw、绝不通知轰炸。
 *
 * 语法约束：ES5（无 let/const/箭头/Set/模板串），目标为模拟器老 WebView 兼容地板。
 */
(function () {
  'use strict';
  if (window.__LINGXI_SHELL_PUSH__) {
    return;
  }
  window.__LINGXI_SHELL_PUSH__ = true;

  var TAG = '[lingxi-shell]';
  var POLL_INTERVAL_MS = 30000;
  var SEEN_CAP = 200;
  var SERVER_BASE_KEY = 'server_base_url';
  var REFRESH_URL = '/api/v1/auth/refresh';
  var REFRESH_KEY = 'refresh_token';
  var PENDING_PATH = '/ext/approval_service/interaction/pending';
  var TASKS_PATH = '/ext/task_service/tasks?limit=100';
  var SEEN_KEY_PENDING = 'lingxi_seen_pending';
  var SEEN_KEY_TASKS = 'lingxi_seen_tasks';
  var CHANNEL_ALERTS = 'lingxi_alerts';
  var CHANNEL_UPDATES = 'lingxi_updates';
  /* 与原生 KernelHostPlugin.FGS_STOP_BUTTON_ID 同值（通知「停止」动作） */
  var STOP_BUTTON_ID = 1002;
  var STATUS_COMPLETED = 'completed';
  var STATUS_FAILED = 'failed';
  var TASK_KEY_DELIMITER = '|';

  function log() {
    var args = [TAG].concat([].slice.call(arguments));
    console.log.apply(console, args);
  }

  function warn() {
    var args = [TAG].concat([].slice.call(arguments));
    console.warn.apply(console, args);
  }

  function plugins() {
    return (window.Capacitor && window.Capacitor.Plugins) || null;
  }

  /* ── 状态栏图标明暗随 app 主题（@capacitor/status-bar）──
   * html.dark 是 app 主题真源（theme-init.js 首帧写、themeService.applyTheme 切换写）。
   * Style.DARK = 状态栏底色暗 → 浅色图标；Style.LIGHT = 底色亮 → 深色图标。
   * 映射为纯函数（契约单测见 app-mobile/tests/shell-push.test.js）。 */
  function statusBarStyle(hasDarkTheme) {
    return hasDarkTheme ? 'DARK' : 'LIGHT';
  }

  var lastStatusBarStyle = null;

  function applyStatusBarStyle() {
    var SB = plugins() && plugins().StatusBar;
    if (!SB || typeof SB.setStyle !== 'function') {
      warn('StatusBar plugin unavailable, theme following disabled');
      return;
    }
    var style = statusBarStyle(document.documentElement.classList.contains('dark'));
    if (style === lastStatusBarStyle) {
      return; /* class 变化未改变明暗（如无关 class 写入）→ 不重发原生调用 */
    }
    /* setStyle 为原生方法名（原生侧 @PluginMethod setStyle，Plugins 桩同样可用）；
     * 只切图标 appearance，不调 setBackgroundColor/setOverlaysWebView（沉浸不动）。
     * 失败不记 last：下轮 class 变化可重试（错误可恢复） */
    SB.setStyle({ style: style }).then(
      function () {
        lastStatusBarStyle = style;
        log('status bar style =', style);
      },
      function (err) {
        warn('status bar setStyle failed:', (err && err.message) || err);
      }
    );
  }

  function watchThemeClass() {
    applyStatusBarStyle(); /* 注入时先对齐现状（theme-init.js 首帧已写 class） */
    if (typeof MutationObserver === 'undefined') {
      warn('MutationObserver unavailable, status bar style follows on reload only');
      return;
    }
    new MutationObserver(function () {
      applyStatusBarStyle();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    log('status bar theme watcher attached');
  }

  /* ── 轮询目标基址（纯函数；与前端连接设置页同键同优先级）──
   * 用户设置（localStorage server_base_url，非空取用）> 构建期注入（shell-config）。
   * 注入面缺席返回 null（调用方跳过本轮），不回落硬编码地址。 */
  function pickBaseUrl(stored, fallback) {
    return typeof stored === 'string' && stored.trim() !== '' ? stored.trim() : fallback;
  }

  /* 构建期注入基址（shell-config.js 由 syncweb 生成、head 经典脚本先于本脚本执行）；
   * 缺失/形态不对 = 无基址（云/本机由构建期单点决定，壳层不自带默认）。 */
  function configBase() {
    var sc = window.LINGXI_SHELL_CONFIG;
    return sc && typeof sc.apiBase === 'string' && sc.apiBase !== '' ? sc.apiBase : null;
  }

  /* ── refresh token 按后端分键（与前端 services/shellApiBase.refreshTokenStorageKey
   * 同契约）──默认档（当前基址=构建期注入，即本机内核档）沿用裸键；自定义后端
   * 按基址隔离 refresh_token@<base>，每后端登录态各自保存、切换往返不互串。 */
  function tokenKeyFor(base) {
    return base && base !== configBase() ? REFRESH_KEY + '@' + base : REFRESH_KEY;
  }

  function currentBase() {
    var stored = null;
    try {
      stored = localStorage.getItem(SERVER_BASE_KEY);
    } catch (e) {
      warn('server_base_url read failed:', e && e.message);
    }
    return pickBaseUrl(stored, configBase());
  }

  /* ── 去重内核（纯函数；契约单测见 app-mobile/tests/shell-push.test.js）──
   * seenList === null/undefined（首启无快照）：news 恒空（快照不轰炸），updatedSeen = current 截尾。
   * 否则：news = current 中未见于 seen（保序），updatedSeen = 去重并集自尾部截 cap 条。 */
  function diffSeen(seenList, currentKeys, cap) {
    var news = [];
    var i;
    var key;
    if (seenList === null || seenList === undefined) {
      return { news: news, updatedSeen: currentKeys.slice(-cap) };
    }
    var merged = [];
    var mark = {};
    for (i = 0; i < seenList.length; i++) {
      key = seenList[i];
      if (!mark[key]) {
        mark[key] = true;
        merged.push(key);
      }
    }
    for (i = 0; i < currentKeys.length; i++) {
      key = currentKeys[i];
      if (!mark[key]) {
        mark[key] = true;
        merged.push(key);
        news.push(key);
      }
    }
    return { news: news, updatedSeen: merged.slice(-cap) };
  }

  function loadSeen(key) {
    try {
      var raw = localStorage.getItem(key);
      if (raw === null) {
        return null; /* 首启无快照 → diffSeen 走快照分支 */
      }
      var arr = JSON.parse(raw);
      return Object.prototype.toString.call(arr) === '[object Array]' ? arr : null;
    } catch (e) {
      warn('seen store corrupt, re-snapshot:', e && e.message);
      return null;
    }
  }

  function saveSeen(key, list) {
    try {
      localStorage.setItem(key, JSON.stringify(list));
    } catch (e) {
      warn('saveSeen failed:', e && e.message);
    }
  }

  function stringHash(s) {
    var h = 0;
    for (var i = 0; i < s.length; i++) {
      h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    }
    return h & 0x7fffffff; /* 非负 int32，作通知 id */
  }

  function fetchJson(url, token, cb) {
    fetch(url, { headers: { Authorization: 'Bearer ' + token } }).then(
      function (resp) {
        if (resp.status === 401) {
          log('401', url, '(token stale, wait for next app open)');
          cb(401, null);
          return;
        }
        if (resp.status !== 200) {
          warn('http', resp.status, url);
          cb(resp.status, null);
          return;
        }
        resp.json().then(
          function (data) {
            cb(200, data);
          },
          function () {
            warn('json parse failed:', url);
            cb(0, null);
          }
        );
      },
      function () {
        warn('network failed:', url);
        cb(0, null);
      }
    );
  }

  /* 通用增量流水：diff → 回写已见集合 → 快照/前台抑制判断 → 通知回调 */
  function processSource(seenKey, keys, onNews) {
    var seen = loadSeen(seenKey);
    var result = diffSeen(seen, keys, SEEN_CAP);
    saveSeen(seenKey, result.updatedSeen);
    if (seen === null) {
      log(seenKey, ': first snapshot,', keys.length, 'marked seen, no notify');
      return;
    }
    if (result.news.length === 0) {
      return;
    }
    log(seenKey, ': new =', result.news.length, '/', keys.length,
      ', appForeground =', document.visibilityState === 'visible');
    if (document.visibilityState === 'visible') {
      return; /* 前台抑制：app 内通知中心自己会显示 */
    }
    onNews(result.news);
  }

  function pollPending(token) {
    fetchJson(currentBase() + PENDING_PATH, token, function (status, data) {
      if (status !== 200 || !data) {
        return;
      }
      var items = data.items || [];
      var keys = [];
      var titles = {};
      for (var i = 0; i < items.length; i++) {
        var item = items[i] || {};
        var md = item.message_data || {};
        var requestId = md.request_id || item.id;
        if (!requestId) {
          continue;
        }
        keys.push(requestId);
        titles[requestId] = md.title || '人类交互请求';
      }
      log('pending round: total =', keys.length);
      processSource(SEEN_KEY_PENDING, keys, function (news) {
        for (var j = 0; j < news.length; j++) {
          notify(news[j], CHANNEL_ALERTS, '灵汐待你审批', titles[news[j]]);
        }
      });
    });
  }

  function pollTasks(token) {
    fetchJson(currentBase() + TASKS_PATH, token, function (status, data) {
      if (status !== 200 || !data) {
        return;
      }
      var items = data.items || [];
      var keys = [];
      var titles = {};
      for (var i = 0; i < items.length; i++) {
        var item = items[i] || {};
        var st = item.status;
        if (st !== STATUS_COMPLETED && st !== STATUS_FAILED) {
          continue;
        }
        var id = item.id;
        if (!id) {
          continue;
        }
        var key = id + TASK_KEY_DELIMITER + st;
        keys.push(key);
        titles[key] = item.title || '（无标题）';
      }
      log('tasks round: terminal total =', keys.length);
      processSource(SEEN_KEY_TASKS, keys, function (news) {
        for (var j = 0; j < news.length; j++) {
          var failed = news[j].slice(-(TASK_KEY_DELIMITER.length + STATUS_FAILED.length)) ===
            TASK_KEY_DELIMITER + STATUS_FAILED;
          notify(
            news[j],
            failed ? CHANNEL_ALERTS : CHANNEL_UPDATES,
            (failed ? '任务失败：' : '任务完成：') + titles[news[j]],
            null
          );
        }
      });
    });
  }

  function notify(key, channelId, title, body) {
    var LN = plugins() && plugins().LocalNotifications;
    /* 前端 bundle 可能未执行 registerPlugin（老 WebView 崩溃），此时 Plugins.X 是
     * native-bridge 按原生方法表生成的桩对象——只有原生方法。v8 插件原生方法为
     * schedule（notify 是纯 JS 别名，桩上不存在），故主用 schedule，兼容旧 notify。 */
    var deliver = null;
    if (LN && typeof LN.schedule === 'function') {
      deliver = function (payload) { return LN.schedule(payload); };
    } else if (LN && typeof LN.notify === 'function') {
      deliver = function (payload) { return LN.notify(payload); };
    }
    if (!deliver) {
      warn('LocalNotifications unavailable, drop notification:', title);
      return;
    }
    deliver({
      notifications: [
        { id: stringHash(key), title: title, body: body || '', channelId: channelId }
      ]
    }).then(
      function () {
        log('notified:', title);
      },
      function (err) {
        warn('notify failed:', (err && err.message) || err);
      }
    );
  }

  /* ── token 换发（D12 后取数契约的根修）──
   * access token 只存主应用内存，壳读不到持久副本；refresh_token 在 localStorage。
   * 每轮先换短时 access token：仅经回调交本轮使用（JS 内存），绝不落盘。
   * 服务端单次轮换：响应的新 refresh_token 必须写回 localStorage 原键——
   * 主应用续期同读此键，漏写会让主应用用已作废旧值续期 401 被登出。
   * 无凭据 / 401 / 网络失败 / 解析失败 → cb(null)，调用方静默跳过本轮。 */
  function refreshAccessToken(cb) {
    var tokenKey = tokenKeyFor(currentBase());
    var rt = null;
    try {
      rt = localStorage.getItem(tokenKey);
    } catch (e) {
      warn('refresh_token read failed:', e && e.message);
    }
    if (!rt) {
      log('skip round: no refresh token yet (waiting for app login)');
      cb(null);
      return;
    }
    fetch(currentBase() + REFRESH_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt })
    }).then(
      function (resp) {
        if (resp.status !== 200) {
          warn('refresh http', resp.status, '- skip round');
          cb(null);
          return;
        }
        resp.json().then(
          function (data) {
            if (!data || !data.access_token) {
              warn('refresh response missing access_token - skip round');
              cb(null);
              return;
            }
            if (data.refresh_token) {
              try {
                localStorage.setItem(tokenKey, data.refresh_token);
              } catch (e2) {
                warn('refresh_token write-back failed:', e2 && e2.message);
              }
            }
            log('token refreshed, expires_in =', data.expires_in);
            cb(data.access_token);
          },
          function () {
            warn('refresh json parse failed - skip round');
            cb(null);
          }
        );
      },
      function () {
        warn('refresh network failed - skip round');
        cb(null);
      }
    );
  }

  function pollRound() {
    if (!currentBase()) {
      warn('skip round: no api base (user setting and shell-config injection both absent)');
      return;
    }
    refreshAccessToken(function (token) {
      if (!token) {
        return;
      }
      pollPending(token);
      pollTasks(token);
    });
  }

  /* ── FGS 通知「停止」动作（FGS 生命周期归原生，JS 只接按钮事件）──
   * 用户点通知上的「停止」→ 插件 buttonClicked 事件 → 调原生 stopLocalStack
   * （SIGTERM 优雅停本地栈 + 压抑制 FGS 至下次栈重启）。 */
  function bindStopButton() {
    var FS = plugins() && plugins().ForegroundService;
    var KH = plugins() && plugins().KernelHost;
    if (!FS || typeof FS.addListener !== 'function') {
      return;
    }
    if (!KH || typeof KH.stopLocalStack !== 'function') {
      return;
    }
    FS.addListener('buttonClicked', function (info) {
      if (info && info.buttonId === STOP_BUTTON_ID) {
        log('user requested local stack stop (notification action)');
        KH.stopLocalStack().then(null, function (err) {
          warn('stopLocalStack failed:', err && err.message);
        });
      }
    });
    log('FGS stop button listener attached');
  }

  function ensureAlertChannels() {
    var LN = plugins() && plugins().LocalNotifications;
    if (!LN || !LN.createChannel) {
      return;
    }
    /* importance 4 = High（待审批/任务失败），3 = Default（任务完成） */
    LN.createChannel({
      id: CHANNEL_ALERTS,
      name: '待办提醒',
      importance: 4,
      description: '待审批与任务失败提醒'
    }).then(null, function (err) {
      warn('alerts channel create failed:', (err && err.message) || err);
    });
    LN.createChannel({
      id: CHANNEL_UPDATES,
      name: '任务动态',
      importance: 3,
      description: '任务完成等普通动态'
    }).then(null, function (err) {
      warn('updates channel create failed:', (err && err.message) || err);
    });
    if (LN.checkPermissions && LN.requestPermissions) {
      LN.checkPermissions().then(function (st) {
        if (st && st.display !== 'granted') {
          LN.requestPermissions().then(
            function (r) {
              log('notification permission requested:', JSON.stringify(r));
            },
            function (err) {
              warn('requestPermissions failed:', (err && err.message) || err);
            }
          );
        }
      }, function (err) {
        warn('checkPermissions failed:', (err && err.message) || err);
      });
    }
  }

  /* ── 后端模式（两档，与连接设置页同键同判）──
   * remote 模式（用户设置 server_base_url 非空=指向远端后端）：壳直连远端，
   * 本地栈不运行——FGS 为保活本地栈存在，此模式不拉前台服务（纯壳档消耗，
   * 与 KernelHostPlugin 冷启动分流同律）。local 模式（无用户设置=壳内置本机
   * 内核）：照旧 FGS 保活。轮询取数两模式共用（基址解析同键）。 */
  function isRemoteMode() {
    var stored = null;
    try {
      stored = localStorage.getItem(SERVER_BASE_KEY);
    } catch (e) {
      warn('server_base_url read failed:', e && e.message);
    }
    return typeof stored === 'string' && stored.trim() !== '';
  }

  function setup() {
    if (typeof Promise === 'undefined' || typeof fetch === 'undefined') {
      warn('WebView too old (no Promise/fetch), shell push disabled');
      return;
    }
    watchThemeClass();
    /* FGS 生命周期归原生（MainActivity onStart/onStop 分流：前台不挂、退后台
     * 才挂，remote 模式恒不挂）；JS 只接通知「停止」按钮事件。 */
    bindStopButton();
    ensureAlertChannels();
    pollRound();
    setInterval(pollRound, POLL_INTERVAL_MS);
    log('polling started, interval ms =', POLL_INTERVAL_MS);
  }

  /* 测试锚点：node 单测直接调用纯函数，不触网络与插件 */
  window.LingxiShellPush = {
    diffSeen: diffSeen,
    loadSeen: loadSeen,
    saveSeen: saveSeen,
    stringHash: stringHash,
    pickBaseUrl: pickBaseUrl,
    configBase: configBase,
    tokenKeyFor: tokenKeyFor,
    isRemoteMode: isRemoteMode,
    refreshAccessToken: refreshAccessToken,
    currentBase: currentBase,
    statusBarStyle: statusBarStyle,
    applyStatusBarStyle: applyStatusBarStyle
  };

  setup();
})();
