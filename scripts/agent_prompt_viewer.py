function renderPrompt(text) {{
  const container = document.getElementById("promptContent");
  const agent = agentsData[currentAgentId];
  const injectMap = (agent && agent.inject_map) ? agent.inject_map : [];
  const MAX_TEXT_CHUNK = 50000;

  if (injectMap.length === 0) {{
    if (currentMode === "markdown") {{
      container.classList.remove("raw");
      container.innerHTML = marked.parse(text || "(空)");
    }} else {{
      container.classList.add("raw");
      container.textContent = text || "(空)";
    }}
    return;
  }}

  const sections = [];
  let lastEnd = 0;
  const sorted = [...injectMap].sort((a, b) => a.start - b.start);

  for (let i = 0; i < sorted.length; i++) {{
    const inj = sorted[i];
    if (inj.start > lastEnd) {{
      sections.push({{ type: "text", content: text.slice(lastEnd, inj.start) }});
    }}
    sections.push({{ type: "inject", name: inj.name, path: inj.path, size: inj.size, content: text.slice(inj.start, inj.end) }});
    lastEnd = inj.end;
  }}
  if (lastEnd < text.length) {{
    sections.push({{ type: "text", content: text.slice(lastEnd) }});
  }}

  let html = "";
  for (const sec of sections) {{
    if (sec.type === "inject") {{
      html += "<div class=\"prompt-inject-section\">";
      html += "<div class=\"prompt-inject-header\" onclick=\"var b=this.nextElementSibling;b.classList.toggle(" + JSON.stringify("collapsed") + ");var a=this.querySelector(" + JSON.stringify(".p-arrow") + ");a.classList.toggle(" + JSON.stringify("collapsed") + ")\">";
      html += "<span class=\"p-arrow\">&#x25BC;</span>";
      html += "<span style=\"flex:1\">&#x1F4C4; " + escapeHtml(sec.name) + "</span>";
      html += "<span style=\"font-size:11px;color:var(--text-secondary)\">" + sec.path + " (" + sec.size.toLocaleString() + " 字符)</span>";
      html += "</div>";
      html += "<div class=\"prompt-inject-body collapsed\">";
      html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:12px;line-height:1.5\">" + escapeHtml(sec.content) + "</pre>";
      html += "</div></div>";
    }} else {{
      if (sec.content.length > MAX_TEXT_CHUNK) {{
        const first = sec.content.slice(0, MAX_TEXT_CHUNK);
        const rest = sec.content.slice(MAX_TEXT_CHUNK);
        if (currentMode === "markdown") {{ html += marked.parse(first); }}
        else {{ html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:13px;line-height:1.6\">" + escapeHtml(first) + "</pre>"; }}
        html += "<div class=\"prompt-inject-section\" style=\"margin-top:8px\">";
        html += "<div class=\"prompt-inject-header\" style=\"background:#FFF3E0;border-color:#FFB74D\" onclick=\"var b=this.nextElementSibling;b.classList.toggle(" + JSON.stringify("collapsed") + ");var a=this.querySelector(" + JSON.stringify(".p-arrow") + ");a.classList.toggle(" + JSON.stringify("collapsed") + ")\">";
        html += "<span class=\"p-arrow\">&#x25BC;</span>";
        html += "<span style=\"flex:1\">&#x26A0;&#xFE0F; 内容过长(" + rest.length.toLocaleString() + " 字符已折叠)</span>";
        html += "</div>";
        html += "<div class=\"prompt-inject-body collapsed\" style=\"max-height:400px;overflow:auto\">";
        html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:12px;line-height:1.5\">" + escapeHtml(rest) + "</pre>";
        html += "</div></div>";
      }} else {{
        if (currentMode === "markdown") {{ html += marked.parse(sec.content); }}
        else {{ html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:13px;line-height:1.6\">" + escapeHtml(sec.content) + "</pre>"; }}
      }}
    }}
  }}
  container.innerHTML = html;
  if (currentMode === "raw") {{ container.classList.add("raw"); }}
  else {{ container.classList.remove("raw"); }}
}}
function toggleSection(secId) {{
  const body = document.getElementById('body-' + secId);
  const arrow = document.getElementById('arrow-' + secId);
  if (body && body.classList.contains('collapsed')) {{
    body.classList.remove('collapsed');
    if (arrow) arrow.classList.remove('collapsed');
  }} else if (body) {{
    body.classList.add('collapsed');
    if (arrow) arrow.classList.add('collapsed');
  }}
  updateToggleAllBtn();
}}

function toggleAllSections() {{
  const bodies = document.querySelectorAll('.inject-section-body');
  const arrows = document.querySelectorAll('.inject-section-header .arrow');
  const btn = document.querySelector('#toggleAllBtn');
  if (window._allExpanded) {{
    bodies.forEach(function(b) {{ b.classList.add('collapsed'); }});
    arrows.forEach(function(a) {{ a.classList.add('collapsed'); }});
    if (btn) btn.textContent = '全部展开';
    window._allExpanded = false;
  }} else {{
    bodies.forEach(function(b) {{ b.classList.remove('collapsed'); }});
    arrows.forEach(function(a) {{ a.classList.remove('collapsed'); }});
    if (btn) btn.textContent = '全部折叠';
    window._allExpanded = true;
  }}
}}

function updateToggleAllBtn() {{
  const bodies = document.querySelectorAll('.inject-section-body');
  const btn = document.querySelector('#toggleAllBtn');
  if (!btn) return;
  const allCollapsed = Array.from(bodies).every(function(b) {{ return b.classList.contains('collapsed'); }});
  btn.textContent = allCollapsed ? '全部展开' : '全部折叠';
  window._allExpanded = !allCollapsed;
}}

function filterAgents() {{t) {{
  const container = document.getElementById("promptContent");
  const agent = agentsData[currentAgentId];
  const injectMap = (agent && agent.inject_map) ? agent.inject_map : [];
  const MAX_TEXT_CHUNK = 50000;

  if (injectMap.length === 0) {{
    if (currentMode === "markdown") {{
      container.classList.remove("raw");
      container.innerHTML = marked.parse(text || "(空)");
    }} else {{
      container.classList.add("raw");
      container.textContent = text || "(空)";
    }}
    return;
  }}

  const sections = [];
  let lastEnd = 0;
  const sorted = [...injectMap].sort((a, b) => a.start - b.start);

  for (let i = 0; i < sorted.length; i++) {{
    const inj = sorted[i];
    if (inj.start > lastEnd) {{
      sections.push({{ type: "text", content: text.slice(lastEnd, inj.start) }});
    }}
    sections.push({{ type: "inject", name: inj.name, path: inj.path, size: inj.size, content: text.slice(inj.start, inj.end) }});
    lastEnd = inj.end;
  }}
  if (lastEnd < text.length) {{
    sections.push({{ type: "text", content: text.slice(lastEnd) }});
  }}

  let html = "";
  for (const sec of sections) {{
    if (sec.type === "inject") {{
      html += "<div class=\"prompt-inject-section\">";
      html += "<div class=\"prompt-inject-header\" onclick=\"var b=this.nextElementSibling;b.classList.toggle(" + JSON.stringify("collapsed") + ");var a=this.querySelector(" + JSON.stringify(".p-arrow") + ");a.classList.toggle(" + JSON.stringify("collapsed") + ")\">";
      html += "<span class=\"p-arrow\">&#x25BC;</span>";
      html += "<span style=\"flex:1\">&#x1F4C4; " + escapeHtml(sec.name) + "</span>";
      html += "<span style=\"font-size:11px;color:var(--text-secondary)\">" + sec.path + " (" + sec.size.toLocaleString() + " 字符)</span>";
      html += "</div>";
      html += "<div class=\"prompt-inject-body collapsed\">";
      html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:12px;line-height:1.5\">" + escapeHtml(sec.content) + "</pre>";
      html += "</div></div>";
    }} else {{
      if (sec.content.length > MAX_TEXT_CHUNK) {{
        const first = sec.content.slice(0, MAX_TEXT_CHUNK);
        const rest = sec.content.slice(MAX_TEXT_CHUNK);
        if (currentMode === "markdown") {{ html += marked.parse(first); }}
        else {{ html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:13px;line-height:1.6\">" + escapeHtml(first) + "</pre>"; }}
        html += "<div class=\"prompt-inject-section\" style=\"margin-top:8px\">";
        html += "<div class=\"prompt-inject-header\" style=\"background:#FFF3E0;border-color:#FFB74D\" onclick=\"var b=this.nextElementSibling;b.classList.toggle(" + JSON.stringify("collapsed") + ");var a=this.querySelector(" + JSON.stringify(".p-arrow") + ");a.classList.toggle(" + JSON.stringify("collapsed") + ")\">";
        html += "<span class=\"p-arrow\">&#x25BC;</span>";
        html += "<span style=\"flex:1\">&#x26A0;&#xFE0F; 内容过长(" + rest.length.toLocaleString() + " 字符已折叠)</span>";
        html += "</div>";
        html += "<div class=\"prompt-inject-body collapsed\" style=\"max-height:400px;overflow:auto\">";
        html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:12px;line-height:1.5\">" + escapeHtml(rest) + "</pre>";
        html += "</div></div>";
      }} else {{
        if (currentMode === "markdown") {{ html += marked.parse(sec.content); }}
        else {{ html += "<pre style=\"margin:0;white-space:pre-wrap;font-size:13px;line-height:1.6\">" + escapeHtml(sec.content) + "</pre>"; }}
      }}
    }}
  }}
  container.innerHTML = html;
  if (currentMode === "raw") {{ container.classList.add("raw"); }}
  else {{ container.classList.remove("raw"); }}
}}

