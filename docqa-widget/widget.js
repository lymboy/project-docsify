/* Doc-QA Widget — floating chat on docsify pages */
(function() {
  'use strict';

  // --- CDN deps (loaded once) ---
  var depsLoaded = false;
  var pendingInit = null;

  function loadDeps(cb) {
    if (depsLoaded) { cb(); return; }
    pendingInit = cb;
    var loaded = 0;
    var needed = 3;
    function done() {
      loaded++;
      if (loaded === needed) { depsLoaded = true; if (pendingInit) pendingInit(); }
    }
    // marked.js
    var s1 = document.createElement('script'); s1.src = '//cdn.jsdelivr.net/npm/marked@12/marked.min.js'; s1.onload = done; document.head.appendChild(s1);
    // highlight.js
    var s2 = document.createElement('script'); s2.src = '//cdn.jsdelivr.net/npm/highlight.js@11/lib/highlight.min.js'; s2.onload = done; document.head.appendChild(s2);
    var l1 = document.createElement('link'); l1.rel = 'stylesheet'; l1.href = '//cdn.jsdelivr.net/npm/highlight.js@11/styles/github-dark-dimmed.min.css'; document.head.appendChild(l1);
    // mermaid
    var s3 = document.createElement('script'); s3.src = '//cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js'; s3.onload = function() {
      mermaid.initialize({ startOnLoad: false, theme: 'default',
        themeVariables: { primaryColor: '#f1f5f9', primaryBorderColor: '#94a3b8', primaryTextColor: '#1e293b', lineColor: '#94a3b8', fontSize: '12px', fontFamily: 'inherit' },
        flowchart: { htmlLabels: true, curve: 'basis' }
      });
      done();
    }; document.head.appendChild(s3);
  }

  // --- Widget HTML ---
  var widget = document.getElementById('docqa-widget');
  widget.innerHTML = [
    '<button id="docqa-fab" title="AI 问答">&#128172;</button>',
    '<div id="docqa-panel" class="docqa-hidden">',
    '  <div id="docqa-resize"></div>',
    '  <div class="docqa-header">',
    '    <div><span class="docqa-header-title"><span>Doc</span>-QA</span>',
    '      <span class="docqa-header-status"><span class="docqa-dot docqa-dot-loading" id="docqa-statusDot"></span><span id="docqa-statusText"></span></span></div>',
    '    <div style="display:flex;gap:4px;align-items:center;">',
    '      <button id="docqa-pin" title="钉住面板" style="background:none;border:none;cursor:pointer;padding:0;transform:rotate(-30deg);opacity:0.3;font-size:15px;color:#64748b;line-height:1;">📌</button>',
    '      <button id="docqa-close" style="background:none;border:none;color:#94a3b8;font-size:20px;cursor:pointer;padding:0;line-height:1;">&times;</button>',
    '    </div>',    '  </div>',
    '  <div class="docqa-chat" id="docqa-chat">',
    '    <div class="docqa-welcome" id="docqa-welcome">',
    '      <h3>文档智能问答</h3>',
    '      <p>基于项目源码推导的知识，覆盖业务规则、枚举值、<br>状态机、配置参数等精确细节。</p>',
    '      <div class="docqa-quick-asks">',
    '        <div class="docqa-quick-ask" data-q="项目整体架构是怎样的？">架构概览</div>',
    '        <div class="docqa-quick-ask" data-q="核心业务流程有哪些？">业务流程</div>',
    '        <div class="docqa-quick-ask" data-q="系统使用了哪些技术组件？">技术组件</div>',
    '      </div>',
    '    </div>',
    '  </div>',
    '  <div class="docqa-input-area">',
    '    <textarea id="docqa-input" placeholder="输入问题，回车发送..." rows="1"></textarea>',
    '    <button class="docqa-send-btn" id="docqa-send">发送</button>',
    '  </div>',
    '</div>'
  ].join('\n');

  // --- DOM refs ---
  var fab = document.getElementById('docqa-fab');
  var panel = document.getElementById('docqa-panel');
  var closeBtn = document.getElementById('docqa-close');
  var chatArea = document.getElementById('docqa-chat');
  var input = document.getElementById('docqa-input');
  var sendBtn = document.getElementById('docqa-send');
  var welcomeEl = document.getElementById('docqa-welcome');
  var pinBtn = document.getElementById('docqa-pin');

  var history = [];
  var loading = false;
  var isOpen = false;
  var isPinned = false;

  // --- Pin toggle ---
  pinBtn.addEventListener('click', function() {
    isPinned = !isPinned;
    if (isPinned) {
      pinBtn.style.transform = 'rotate(0deg)';
      pinBtn.style.opacity = '1';
      pinBtn.style.color = '#e11d48';
      pinBtn.title = '取消钉住';
    } else {
      pinBtn.style.transform = 'rotate(-30deg)';
      pinBtn.style.opacity = '0.3';
      pinBtn.style.color = '#64748b';
      pinBtn.title = '钉住面板';
    }
  });

  // --- Toggle ---
  fab.addEventListener('click', function() {
    isOpen = !isOpen;
    if (isOpen) {
      panel.classList.remove('docqa-hidden');
      input.focus();
      loadDeps(initChat);
    } else {
      panel.classList.add('docqa-hidden');
    }
  });
  closeBtn.addEventListener('click', function() {
    panel.classList.add('docqa-hidden');
    isOpen = false;
  });

  // --- Quick asks ---
  widget.addEventListener('click', function(e) {
    var qa = e.target.closest('.docqa-quick-ask');
    if (qa) { input.value = qa.getAttribute('data-q'); send(); }
  });

  // --- Input ---
  input.addEventListener('input', function() {
    input.style.height = '38px';
    input.style.height = Math.min(input.scrollHeight, 100) + 'px';
  });
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener('click', send);

  // --- Status ---
  fetch('/api/status').then(function(r){return r.json()}).then(function(data) {
    if (data.status !== 'ready') return;
    var dot = document.getElementById('docqa-statusDot');
    var text = document.getElementById('docqa-statusText');
    var emb = data.metadata.embedding === 'enabled';
    dot.className = 'docqa-dot ' + (emb ? 'docqa-dot-loading' : 'docqa-dot-ok');
    var label = data.metadata.section_count + ' 章节';
    if (emb) label += ' · 语义';
    if (data.metadata.link_graph_nodes) label += ' · 图';
    text.textContent = label;
  }).catch(function(){});

  // --- Chat init (after deps loaded) ---
  var chatInited = false;
  function initChat() {
    if (chatInited) return;
    chatInited = true;
    marked.setOptions({
      highlight: function(code, lang) {
        if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, {language: lang}).value;
        return hljs.highlightAuto(code).value;
      },
      breaks: true, gfm: true,
    });
  }

  // --- Send ---
  function send() {
    var q = input.value.trim();
    if (!q || loading) return;
    if (welcomeEl) { welcomeEl.remove(); welcomeEl = null; }

    addMsg('user', q);
    input.value = '';
    input.style.height = '38px';
    loading = true;
    sendBtn.disabled = true;

    var loadId = addMsg('ai', '', true);
    sendStreaming(q, loadId);
  }

  // --- SSE streaming ---
  function sendStreaming(question, loadId) {
    var rawText = '';
    var sources = [];
    var graphExpanded = false;
    var budgetInfo = '';
    var msgId = null;

    fetch('/api/query/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: question, history: history.slice(-6) })
    }).then(function(response) {
      if (!response.ok) throw new Error('HTTP ' + response.status);
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';

      function read() {
        return reader.read().then(function(result) {
          if (result.done) {
            finalizeMessage(msgId, rawText, sources, loadId, graphExpanded, budgetInfo);
            history.push({ role: 'user', content: question });
            history.push({ role: 'assistant', content: rawText });
            loading = false;
            sendBtn.disabled = false;
            input.focus();
            return;
          }
          buffer += decoder.decode(result.value, { stream: true });
          var lines = buffer.split('\n\n');
          buffer = lines.pop();

          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (line.indexOf('data: ') !== 0) continue;
            var data = line.slice(6);
            try {
              var parsed = JSON.parse(data);
              if (parsed.type === 'sources') {
                sources = parsed.sources;
                graphExpanded = parsed.graph_expanded || false;
              } else if (parsed.type === 'budget') {
                budgetInfo = parsed.pages_tokens + ' → ' + parsed.total_budget + ' tokens';
              } else if (parsed.type === 'token') {
                rawText += parsed.content;
                if (!msgId) {
                  removeMsg(loadId);
                  msgId = addMsg('ai', '', false, true);
                }
                updateStreamingMessage(msgId, rawText);
              } else if (parsed.type === 'error') {
                if (!msgId) { removeMsg(loadId); msgId = addMsg('ai', parsed.content); }
                else { rawText += '\n\n' + parsed.content; updateStreamingMessage(msgId, rawText); }
              }
            } catch(e) {}
          }
          return read();
        });
      }
      return read();
    }).catch(function(err) {
      removeMsg(loadId);
      addMsg('ai', '请求失败: ' + err.message);
      loading = false;
      sendBtn.disabled = false;
    });
  }

  // --- Message rendering ---
  function normalizeMarkdown(text) {
    // Compress 3+ consecutive newlines into 2 (one visual blank line)
    var t = text.replace(/\n{3,}/g, '\n\n');
    // Collapse blank lines between list items (keeps list compact)
    t = t.replace(/(\n[-*]\s.+)\n\n(?=[-*]\s)/g, '$1\n');
    t = t.replace(/(\n\d+\.\s.+)\n\n(?=\d+\.\s)/g, '$1\n');
    // Collapse blank lines between table rows
    t = t.replace(/(\n\|.+\|)\n\n(?=\|.+\|)/g, '$1\n');
    return t;
  }

  function docsifyUrl(sourceFile) {
    return '#/' + sourceFile.replace(/\.md$/, '');
  }

  function addMsg(role, content, isLoading, isStreaming) {
    var id = 'docqa-msg-' + Date.now() + Math.random().toString(36).slice(2, 6);
    var div = document.createElement('div');
    div.className = 'docqa-msg docqa-msg-' + role;
    div.id = id;

    var avatar = document.createElement('div');
    avatar.className = 'docqa-msg-avatar';
    avatar.textContent = role === 'user' ? '你' : 'AI';

    var body = document.createElement('div');
    body.className = 'docqa-msg-body';
    var bubble = document.createElement('div');
    bubble.className = 'docqa-msg-bubble';

    if (isLoading) {
      bubble.innerHTML = '<div class="docqa-typing"><span></span><span></span><span></span></div>';
    } else if (isStreaming) {
      bubble.innerHTML = '<span class="docqa-stream-cursor"></span>';
    } else if (role === 'user') {
      bubble.textContent = content;
    } else {
      var normalized = normalizeMarkdown(content);
      bubble.innerHTML = marked.parse(normalized);
      highlightCitations(bubble);
      renderMermaidInContainer(bubble);
    }

    body.appendChild(bubble);
    div.appendChild(avatar);
    div.appendChild(body);
    chatArea.appendChild(div);
    chatArea.scrollTop = chatArea.scrollHeight;
    return id;
  }

  function removeMsg(id) {
    var el = document.getElementById(id);
    if (el) el.remove();
  }

  function updateStreamingMessage(id, text) {
    var el = document.getElementById(id);
    if (!el) return;
    var bubble = el.querySelector('.docqa-msg-bubble');
    if (!bubble) return;
    var normalized = normalizeMarkdown(text);
    bubble.innerHTML = marked.parse(normalized) + '<span class="docqa-stream-cursor"></span>';
    chatArea.scrollTop = chatArea.scrollHeight;
  }

  function finalizeMessage(msgId, rawText, sources, loadId, graphExpanded, budgetInfo) {
    if (!msgId) { removeMsg(loadId); msgId = addMsg('ai', rawText || '未收到响应'); return; }
    var msgEl = document.getElementById(msgId);
    if (!msgEl) return;

    var normalized = normalizeMarkdown(rawText);
    var bubble = msgEl.querySelector('.docqa-msg-bubble');
    if (bubble) {
      bubble.innerHTML = marked.parse(normalized);
      highlightCitations(bubble);
      renderMermaidInContainer(bubble);
    }

    var cursor = msgEl.querySelector('.docqa-stream-cursor');
    if (cursor) cursor.remove();

    var body = msgEl.querySelector('.docqa-msg-body');
    if (sources && sources.length) {
      var srcDiv = document.createElement('div');
      srcDiv.className = 'docqa-msg-sources';
      srcDiv.textContent = '来源: ';
      sources.forEach(function(s) {
        var a = document.createElement('a');
        a.href = docsifyUrl(s);
        a.textContent = s;
        a.addEventListener('click', function(e) {
          // Auto-collapse panel so user sees docsify navigation (unless pinned)
          if (!isPinned) {
            panel.classList.add('docqa-hidden');
            isOpen = false;
          }
        });
        srcDiv.appendChild(a);
      });
      if (graphExpanded) {
        var badge = document.createElement('a');
        badge.className = 'docqa-graph-badge';
        badge.textContent = '图扩展';
        badge.href = '#';
        badge.addEventListener('click', function(e) { e.preventDefault(); });
        srcDiv.appendChild(badge);
      }
      body.appendChild(srcDiv);
    }
  }

  // --- Citation highlighting ---
  function highlightCitations(container) {
    var walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    var textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);

    textNodes.forEach(function(node) {
      var text = node.textContent;
      if (text.indexOf('[来源:') < 0 && text.indexOf('[未验证]') < 0) return;

      var frag = document.createDocumentFragment();
      var remaining = text;

      while (remaining) {
        var citeIdx = remaining.indexOf('[来源:');
        var unvIdx = remaining.indexOf('[未验证]');
        var nextIdx = -1;
        var isCite = false;

        if (citeIdx >= 0 && (unvIdx < 0 || citeIdx <= unvIdx)) { nextIdx = citeIdx; isCite = true; }
        else if (unvIdx >= 0) { nextIdx = unvIdx; isCite = false; }

        if (nextIdx < 0) { frag.appendChild(document.createTextNode(remaining)); break; }
        if (nextIdx > 0) frag.appendChild(document.createTextNode(remaining.slice(0, nextIdx)));

        if (isCite) {
          var closeIdx = remaining.indexOf(']', nextIdx + 3);
          if (closeIdx >= 0) {
            var citeText = remaining.slice(nextIdx, closeIdx + 1);
            var contentMatch = citeText.match(/\[来源:\s*(.+)\s*\]/);
            if (contentMatch) {
              var files = contentMatch[1].split(',').map(function(f) { return f.trim(); }).filter(function(f) { return /\.md$/.test(f); });
              files.forEach(function(file) {
                var a = document.createElement('a');
                a.className = 'docqa-msg-cite';
                a.textContent = '[来源: ' + file + ']';
                a.href = docsifyUrl(file);
                a.addEventListener('click', function() {
                  if (!isPinned) {
                    panel.classList.add('docqa-hidden');
                    isOpen = false;
                  }
                });
                frag.appendChild(a);
              });
            } else {
              var span = document.createElement('span');
              span.className = 'docqa-msg-cite';
              span.textContent = citeText;
              frag.appendChild(span);
            }
            remaining = remaining.slice(closeIdx + 1);
          } else {
            frag.appendChild(document.createTextNode(remaining));
            break;
          }
        } else {
          var span = document.createElement('span');
          span.className = 'docqa-msg-unverified';
          span.textContent = '[未验证]';
          frag.appendChild(span);
          remaining = remaining.slice(nextIdx + 5);
        }
      }
      node.parentNode.replaceChild(frag, node);
    });
  }

  // --- Mermaid rendering ---
  function renderMermaidInContainer(container) {
    var codeBlocks = container.querySelectorAll('code.language-mermaid');
    codeBlocks.forEach(function(block) {
      var pre = block.parentElement;
      if (!pre || pre.tagName !== 'PRE') return;
      var code = block.textContent;
      var id = 'docqa-mermaid-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);
      mermaid.render(id, code).then(function(result) {
        var wrapper = document.createElement('div');
        wrapper.className = 'docqa-mermaid-diagram';
        wrapper.innerHTML = result.svg;
        pre.replaceWith(wrapper);
      }).catch(function() {
        var errDiv = document.createElement('div');
        errDiv.className = 'docqa-mermaid-diagram';
        errDiv.style.cssText = 'color: #64748b; font-size: 11px;';
        errDiv.textContent = '[图表渲染失败]';
        pre.replaceWith(errDiv);
      });
    });
  }

  // --- Resize: drag from top-left corner ---
  var resizeHandle = document.getElementById('docqa-resize');
  var isResizing = false;
  var resizeStartX, resizeStartY, startWidth, startHeight, startRight, startBottom;

  resizeHandle.addEventListener('mousedown', function(e) {
    e.preventDefault();
    isResizing = true;
    var rect = panel.getBoundingClientRect();
    resizeStartX = e.clientX;
    resizeStartY = e.clientY;
    startWidth = rect.width;
    startHeight = rect.height;
    document.body.style.cursor = 'nwse-resize';
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', function(e) {
    if (!isResizing) return;
    var dx = resizeStartX - e.clientX;  // drag left = increase width
    var dy = resizeStartY - e.clientY;  // drag up = increase height
    var newW = Math.min(Math.max(startWidth + dx, 300), window.innerWidth * 0.9);
    var newH = Math.min(Math.max(startHeight + dy, 400), window.innerHeight * 0.85);
    panel.style.width = newW + 'px';
    panel.style.height = newH + 'px';
  });

  document.addEventListener('mouseup', function() {
    if (isResizing) {
      isResizing = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    }
  });

})();
