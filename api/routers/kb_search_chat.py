"""知识库搜索 Chatbox — 用于交互式测试向量搜索效果。"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["search"])


class SearchRequest(BaseModel):
    query: str
    kb_ids: list[str]
    mode: str = "FAST"


@router.post("/api/v1/search/vector")
def vector_search(req: SearchRequest):
    """向量搜索。"""
    from services.vector_search import search as do_search
    results = do_search(req.kb_ids, req.query, max_results=3)
    return {"results": results}


@router.get("/api/v1/search/knowledge-bases")
def list_kbs():
    import storage.kb_repo as kb_repo
    kbs = kb_repo.list_all()
    return {
        "knowledge_bases": [
            {"id": kb.id, "name": kb.name, "category": kb.category}
            for kb in kbs
        ]
    }


@router.get("/api/v1/search/text")
def text_search(query: str, kb_ids: str):
    from services.vector_search import _text_search, _get_kb_search_paths
    ids = [k.strip() for k in kb_ids.split(",") if k.strip()]
    paths = _get_kb_search_paths(ids)
    content = _text_search(paths, [query], max_results=3)
    if content:
        return {"results": [{"source": "text_search", "content": content, "relevance": 0.6}]}
    return {"results": []}


@router.get("/search-chat")
def chat_page():
    html = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>知识库搜索 Chatbox</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f5;height:100vh;display:flex;flex-direction:column}
.header{background:#2d2d2d;color:#fff;padding:12px 20px;display:flex;align-items:center;gap:12px}
.header h1{font-size:18px;font-weight:600}
.header .subtitle{font-size:12px;color:#aaa}
.toolbar{background:#fff;padding:10px 20px;border-bottom:1px solid #e0e0e0}
.toolbar-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.toolbar label{font-size:13px;color:#555;font-weight:600}
.kb-chip{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border:1px solid #ccc;border-radius:14px;font-size:12px;cursor:pointer;background:#fff;user-select:none;transition:all .15s}
.kb-chip:hover{border-color:#0066ff;background:#f0f5ff}
.kb-chip.selected{background:#0066ff;color:#fff;border-color:#0066ff}
.kb-chip .check{display:inline-block;width:14px;text-align:center;font-size:11px}
.mode-group{display:flex;gap:4px}
.mode-btn{padding:4px 12px;border:1px solid #ccc;border-radius:4px;background:#fff;cursor:pointer;font-size:12px}
.mode-btn.active{background:#0066ff;color:#fff;border-color:#0066ff}
.chat-area{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;gap:16px}
.msg{max-width:85%;padding:12px 16px;border-radius:12px;font-size:14px;line-height:1.6}
.msg.user{align-self:flex-end;background:#0066ff;color:#fff;border-bottom-right-radius:4px}
.msg.assistant{align-self:flex-start;background:#fff;color:#333;border-bottom-left-radius:4px;box-shadow:0 1px 3px rgba(0,0,0,.1);white-space:pre-wrap}
.msg .meta{font-size:11px;color:#999;margin-bottom:4px}
.msg .source-tag{display:inline-block;font-size:11px;background:#e8f0fe;color:#0066ff;padding:2px 8px;border-radius:10px;margin-bottom:6px}
.input-area{background:#fff;padding:12px 20px;border-top:1px solid #e0e0e0;display:flex;gap:10px;align-items:center}
.input-area input{flex:1;padding:10px 14px;border:1px solid #ddd;border-radius:24px;font-size:14px;outline:none}
.input-area input:focus{border-color:#0066ff}
.input-area button{padding:10px 24px;background:#0066ff;color:#fff;border:none;border-radius:24px;font-size:14px;cursor:pointer}
.input-area button:disabled{background:#ccc}
.spinner{display:inline-block;width:16px;height:16px;border:2px solid #ccc;border-top-color:#0066ff;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="header">
  <h1>知识库搜索 Chatbox</h1>
  <span class="subtitle">交互式知识库搜索测试</span>
</div>
<div class="toolbar">
  <div class="toolbar-row">
    <label>知识库：</label>
    <span id="kb-container"></span>
  </div>
  <div class="toolbar-row">
    <label>模式：</label>
    <div class="mode-group">
      <button class="mode-btn active" data-mode="FAST">FAST</button>
      <button class="mode-btn" data-mode="FILENAME_ONLY">文件名</button>
      <button class="mode-btn" data-mode="TEXT">文本搜索</button>
    </div>
  </div>
</div>
<div class="chat-area" id="chat-area">
  <div class="msg assistant">你好！选择知识库（点击切换），输入查询内容开始测试。</div>
</div>
<div class="input-area">
  <input id="query-input" type="text" placeholder="输入搜索内容..." />
  <button id="send-btn">发送</button>
</div>

<script>
let currentMode = 'FAST';
let selectedKbs = new Set();

// 模式按钮
document.querySelectorAll('.mode-btn').forEach(function(b) {
  b.addEventListener('click', function() {
    currentMode = this.dataset.mode;
    document.querySelectorAll('.mode-btn').forEach(function(x) {
      x.classList.toggle('active', x.dataset.mode === currentMode);
    });
  });
});

// 加载知识库
fetch('/api/v1/search/knowledge-bases')
.then(function(r) { return r.json(); })
.then(function(data) {
  var container = document.getElementById('kb-container');
  container.innerHTML = '';
  data.knowledge_bases.forEach(function(kb) {
    selectedKbs.add(kb.id);
    var chip = document.createElement('span');
    chip.className = 'kb-chip selected';
    chip.innerHTML = '<span class=\"check\">&#10003;</span> ' + kb.name;
    chip.addEventListener('click', function() {
      if (selectedKbs.has(kb.id)) {
        selectedKbs.delete(kb.id);
        chip.classList.remove('selected');
        chip.innerHTML = '<span class="check">&nbsp;</span> ' + kb.name;
      } else {
        selectedKbs.add(kb.id);
        chip.classList.add('selected');
        chip.innerHTML = '<span class="check">&#10003;</span> ' + kb.name;
      }
    });
    container.appendChild(chip);
  });
});

// 发送
function addMessage(role, html, meta) {
  var area = document.getElementById('chat-area');
  var div = document.createElement('div');
  div.className = 'msg ' + role;
  var inner = '';
  if (meta) inner += '<div class="meta">' + meta + '</div>';
  inner += html;
  div.innerHTML = inner;
  area.appendChild(div);
  area.scrollTop = area.scrollHeight;
}

document.getElementById('send-btn').addEventListener('click', function() {
  var input = document.getElementById('query-input');
  var query = input.value.trim();
  if (!query) return;

  if (selectedKbs.size === 0) {
    addMessage('assistant', '请先选择至少一个知识库。');
    return;
  }

  addMessage('user', query);
  input.value = '';
  this.disabled = true;

  var loading = addMessage('assistant', '<span class="spinner"></span> 搜索中...');

  var url, body;
  if (currentMode === 'TEXT') {
    url = '/api/v1/search/text?query=' + encodeURIComponent(query) + '&kb_ids=' + encodeURIComponent(Array.from(selectedKbs).join(','));
    fetch(url).then(function(r) { return r.json(); }).then(showResults).catch(showError);
  } else {
    fetch('/api/v1/search/vector', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: query, kb_ids: Array.from(selectedKbs), mode: currentMode }),
    }).then(function(r) { return r.json(); }).then(showResults).catch(showError);
  }

  function showResults(data) {
    loading.remove();
    var results = data.results || [];
    if (results.length === 0) {
      addMessage('assistant', '未找到相关内容。');
      return;
    }
    results.forEach(function(r) {
      var tag = r.source === 'vector_search' ? '向量检索' : r.source === 'text_search' ? '文本搜索' : r.source;
      var text = (r.content || '').slice(0, 2000);
      text = text.replace(/</g, '&lt;').replace(/>/g, '&gt;');
      addMessage('assistant', '<div class="source-tag">' + tag + '</div>' + text);
    });
    document.getElementById('send-btn').disabled = false;
  }

  function showError(err) {
    loading.remove();
    addMessage('assistant', '搜索出错: ' + err.message);
    document.getElementById('send-btn').disabled = false;
  }
});

// Enter 键发送
document.getElementById('query-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') document.getElementById('send-btn').click();
});
</script>
</body>
</html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)
