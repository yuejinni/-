/**
 * 报关文件生成器 - Content Script
 * 所有函数定义在顶层，避免 else{} 块内函数声明的提升问题
 */

const SERVER      = 'http://localhost:5008';
const STORAGE_KEY = 'customs_settings';
const IS_TOP_FRAME       = (window === window.top);
const IS_IFRAME_TRANSFER = !IS_TOP_FRAME && location.href.includes('transfers.jsp');

/* ═══════════════════════════════════════════════════════════════════
   工具：找到包含调拨单表格的 Document
   ═══════════════════════════════════════════════════════════════════ */
function getTransferDoc() {
  if (document.querySelector('td.goods')) return document;
  const iframes = document.querySelectorAll('iframe');
  for (const iframe of iframes) {
    try {
      const d = iframe.contentDocument || iframe.contentWindow?.document;
      if (d && d.querySelector('td.goods')) return d;
    } catch {}
  }
  return null;
}

/* ═══════════════════════════════════════════════════════════════════
   工具：从调拨单 DOM 抓取商品数据
   ═══════════════════════════════════════════════════════════════════ */
function scrapeItems(doc) {
  if (!doc) return [];

  const goodsCells = Array.from(doc.querySelectorAll('td.goods')).filter(
    el => el.offsetParent !== null  // 过滤隐藏 tab 里的元素
  );
  if (!goodsCells.length) return [];

  const items = [];
  goodsCells.forEach((cell) => {
    const title = (cell.getAttribute('title') || cell.textContent || '').trim();
    const code  = title.split(/\s+/)[0];
    if (!code || !/^[A-Za-z0-9]/.test(code)) return;

    const tr = cell.closest('tr');
    if (!tr) return;

    // 读取单元格值：优先 td title 属性，备选 input.value
    function tdVal(key) {
      const tds = Array.from(tr.querySelectorAll('td[aria-describedby]'));
      const td  = tds.find(t => (t.getAttribute('aria-describedby') || '').includes(key));
      if (td) {
        const t = (td.getAttribute('title') || td.textContent || '').trim();
        if (t) return t;
      }
      const inp = tr.querySelector(`input[name="${key}"]`);
      return inp ? (inp.value || '').trim() : '';
    }

    const unit     = tdVal('mainUnit');
    const descVal  = tdVal('description');
    const rmbMatch = descVal.match(/[¥￥]([\d.]+)/);
    const rmbPrice = rmbMatch ? parseFloat(rmbMatch[1]) : 0;

    // 读取装箱总件数（优先从专用列读取）
    let boxes = 0;
    const allTds = Array.from(tr.querySelectorAll('td[aria-describedby]'));
    for (const td of allTds) {
      const desc = td.getAttribute('aria-describedby') || '';
      // 匹配可能包含箱数/件数/整件的列
      if (desc.includes('totalBox') || desc.includes('totalCarton') || desc.includes('boxQty')
          || desc.includes('packageQty') || desc.includes('wholePiece') || desc.includes('totalPackage')
          || desc.includes('zjp') || desc.includes('cartonQty') || desc.includes('boxNum')) {
        const raw = (td.getAttribute('title') || td.textContent || '').trim();
        const v = parseFloat(raw.replace(/,/g, ''));
        if (!isNaN(v) && v > 0) { boxes = v; break; }
      }
    }

    // 读取数量（排除箱数/包装列，避免 packageQty 等被误匹配为 qty）
    let qty = 0;
    const _boxKeywords = ['totalBox', 'totalCarton', 'boxQty', 'packageQty',
      'wholePiece', 'totalPackage', 'zjp', 'cartonQty', 'boxNum', 'package', 'carton', 'box'];
    const qtyTds = Array.from(tr.querySelectorAll('td[aria-describedby]'));
    for (const td of qtyTds) {
      const desc = td.getAttribute('aria-describedby') || '';
      const isBoxCol = _boxKeywords.some(kw => desc.toLowerCase().includes(kw.toLowerCase()));
      if (!isBoxCol && (desc.includes('qty') || desc.includes('Qty') || desc.includes('number'))) {
        const raw = (td.getAttribute('title') || td.textContent || '').trim();
        const v   = parseFloat(raw.replace(/,/g, ''));
        if (!isNaN(v) && v > 0) { qty = v; break; }
      }
    }
    if (!qty) {
      const qtyIn = tr.querySelector('input[name="qty"], input[name="number"], input[name="quantity"]');
      if (qtyIn) { const v = parseFloat((qtyIn.value || '').replace(/,/g, '')); if (!isNaN(v) && v > 0) qty = v; }
    }
    if (!qty) {
      for (const td of tr.querySelectorAll('td[role="gridcell"]')) {
        if (td === cell) continue;
        const txt = (td.getAttribute('title') || td.textContent || '').trim();
        const v   = parseFloat(txt.replace(/,/g, ''));
        if (!isNaN(v) && v > 0 && v < 10000 && txt.length < 9) { qty = v; break; }
      }
    }
    if (!qty) qty = 1;

    items.push({ code, unit, qty, rmb_price: rmbPrice, desc: descVal, boxes: boxes });
  });

  return items;
}

/* ═══════════════════════════════════════════════════════════════════
   工具：通过 background service worker 代理 HTTP 请求
   （content.js 在公网页面上下文，直接 fetch localhost 被 Chrome PNA 拦截）
   ═══════════════════════════════════════════════════════════════════ */
function serverFetch(url, options) {
  const opt = options || {};
  return new Promise(function(resolve, reject) {
    try {
      chrome.runtime.sendMessage({
        type:    'SERVER_FETCH',
        url:     url,
        method:  opt.method  || 'GET',
        headers: opt.headers || {},
        body:    opt.body    || null,
        timeout: opt.timeout || 30000,
      }, function(resp) {
        if (chrome.runtime.lastError) {
          const msg = chrome.runtime.lastError.message || '';
          if (msg.includes('invalidated') || msg.includes('context')) {
            reject(new Error('扩展已更新，请刷新页面后重试'));
          } else {
            reject(new Error(msg));
          }
          return;
        }
        if (resp && resp.ok) resolve(resp.data);
        else reject(new Error((resp && resp.error) || '服务器错误'));
      });
    } catch (e) {
      reject(new Error('扩展已更新，请刷新页面后重试'));
    }
  });
}

/* ═══════════════════════════════════════════════════════════════════
   工具：从采购订单页面抓取商品图片
   ═══════════════════════════════════════════════════════════════════ */
function scrapePurchaseImages() {
  // 先找到含有商品图片的 document（可能在 iframe 里）
  function findDoc() {
    if (document.querySelector('td.img img')) return document;
    var iframes = document.querySelectorAll('iframe');
    for (var i = 0; i < iframes.length; i++) {
      try {
        var d = iframes[i].contentDocument || iframes[i].contentWindow.document;
        if (d && d.querySelector('td.img img')) return d;
      } catch(e) {}
    }
    return null;
  }

  var doc = findDoc();
  if (!doc) return [];

  var rows = Array.from(doc.querySelectorAll('tr.ui-widget-content'));
  var seen = {};
  var items = [];

  rows.forEach(function(row) {
    var img = row.querySelector('td.img img');
    if (!img || !img.src || !img.src.startsWith('http')) return;

    // 15% 缩略图换成 50% 以提升 AI 识别率
    var imgUrl = img.src.replace(/(!)\d+p/, '$150p');

    // 从行内 td 文本里匹配商品编码（如 T.F240T-307）
    var code = '', name = '', category = '';
    var tds = Array.from(row.querySelectorAll('td'));
    for (var i = 0; i < tds.length; i++) {
      var td = tds[i];
      if (td.classList.contains('img')) continue;
      var text = (td.getAttribute('title') || td.textContent || '').trim();
      var m = text.match(/^([A-Za-z][A-Za-z0-9._-]*\d[A-Za-z0-9._-]*)/);
      if (m) {
        code = m[1];
        name = text.slice(code.length).trim();
        break;
      }
    }
    if (!code || seen[code]) return;

    // 取商品类别（含英文字母、长度 > 3、非编码列）
    for (var j = 0; j < tds.length; j++) {
      var t = (tds[j].getAttribute('title') || tds[j].textContent || '').trim();
      if (t && t !== code && t.length > 3 && /[a-zA-Z]/.test(t) && !/^\d/.test(t) && t !== name) {
        category = t; break;
      }
    }

    seen[code] = true;
    items.push({ code: code, name: name, category: category, imgUrl: imgUrl });
  });

  return items;
}

/* ═══════════════════════════════════════════════════════════════════
   UI：构建侧边栏 HTML
   ═══════════════════════════════════════════════════════════════════ */
function buildSidebar() {
  const toggleBtn = document.createElement('button');
  toggleBtn.id    = 'customs-toggle-btn';
  toggleBtn.title = '报关文件生成器';
  toggleBtn.innerHTML = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <rect x="4" y="2" width="16" height="20" rx="2" fill="none" stroke="#fff" stroke-width="2"/>
    <line x1="8" y1="8"  x2="16" y2="8"  stroke="#fff" stroke-width="1.5"/>
    <line x1="8" y1="12" x2="16" y2="12" stroke="#fff" stroke-width="1.5"/>
    <line x1="8" y1="16" x2="13" y2="16" stroke="#fff" stroke-width="1.5"/>
  </svg>`;

  const sidebar = document.createElement('div');
  sidebar.id    = 'customs-sidebar';
  sidebar.innerHTML = `
    <div class="cs-header">
      <div class="cs-header-title">
        <svg viewBox="0 0 24 24" style="width:18px;height:18px;fill:none;stroke:#fff;stroke-width:2">
          <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/>
          <rect x="9" y="3" width="6" height="4" rx="1"/>
        </svg>
        报关文件生成器
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span class="cs-server-dot" id="cs-dot" title="服务器状态"></span>
        <button class="cs-close-btn" id="cs-close">×</button>
      </div>
    </div>

    <div class="cs-tabs">
      <button class="cs-tab active" id="cs-tab-gen">报关生成</button>
      <button class="cs-tab" id="cs-tab-ai">AI识别基础信息</button>
    </div>

    <!-- Tab 1: 报关生成 -->
    <div id="cs-panel-gen" class="cs-tab-panel cs-tab-panel--active">
      <div class="cs-items-section">
        <div class="cs-section-label" id="cs-items-label">点击「刷新商品」读取调拨单</div>
        <div id="cs-items-list"><div class="cs-no-data">暂未读取到商品</div></div>
      </div>
      <div class="cs-form-section">
        <div class="cs-form-row">
          <span class="cs-form-label">汇率 CNY/USD</span>
          <input class="cs-form-input" id="cs-rate" type="number" step="0.01" placeholder="7.20" value="7.20">
        </div>
        <div class="cs-form-row">
          <span class="cs-form-label">发票号</span>
          <input class="cs-form-input" id="cs-invoice" type="text" placeholder="LK00022">
        </div>
        <div class="cs-form-row">
          <span class="cs-form-label">目标总额 USD</span>
          <input class="cs-form-input" id="cs-target" type="number" step="1" placeholder="可选，留空不调整">
        </div>
        <div class="cs-form-row">
          <span class="cs-form-label">过磅总重量 KGS</span>
          <input class="cs-form-input" id="cs-gw-total" type="number" step="0.1" placeholder="可选，留空按基础资料计算">
        </div>
        <div class="cs-form-row">
          <span class="cs-form-label">保存路径</span>
          <input class="cs-form-input" id="cs-path" type="text" placeholder="点击 📁 选择文件夹">
          <button type="button" class="cs-btn-browse" id="cs-browse-btn" title="选择文件夹">📁</button>
        </div>
      </div>
      <div class="cs-footer">
        <div class="cs-total-row">
          <span>USD 合计</span>
          <span class="cs-total-amount" id="cs-total">$0.00</span>
        </div>
        <div class="cs-btn-row">
          <button type="button" class="cs-btn cs-btn-secondary" id="cs-refresh-btn">🔄 刷新商品</button>
          <button type="button" class="cs-btn cs-btn-primary"   id="cs-generate-btn" disabled>生成文件 →</button>
        </div>
        <div class="cs-status info" id="cs-status"></div>
      </div>
    </div>

    <!-- Tab 2: AI识别基础信息 -->
    <div id="cs-panel-ai" class="cs-tab-panel">
      <div class="cs-ai-section">

        <!-- ① 从页面批量读取 -->
        <button type="button" class="cs-btn cs-btn-secondary" id="cs-ai-scrape" style="width:100%">
          📋 读取页面商品图片
        </button>
        <div id="cs-batch-wrap" style="display:none">
          <div style="display:flex;align-items:center;justify-content:space-between;margin:6px 0 4px">
            <span class="cs-section-label" style="margin:0">找到 <span id="cs-batch-count">0</span> 件</span>
            <button type="button" class="cs-btn cs-btn-primary" id="cs-batch-all"
              style="height:26px;font-size:11px;padding:0 10px;flex:none">全部识别</button>
          </div>
          <div id="cs-batch-list"></div>
        </div>

        <!-- ② 手动单条 -->
        <div class="cs-ai-divider">— 或手动填写单条 —</div>
        <div class="cs-form-row">
          <span class="cs-form-label">图片 URL</span>
          <input class="cs-form-input" id="cs-ai-url" type="text" placeholder="https://...（右键复制图片地址）">
        </div>
        <div class="cs-form-row">
          <span class="cs-form-label">商品编码</span>
          <input class="cs-form-input" id="cs-ai-code" type="text" placeholder="T.F240T-308">
        </div>
        <div class="cs-form-row">
          <span class="cs-form-label">商品名称</span>
          <input class="cs-form-input" id="cs-ai-name" type="text" placeholder="可选，辅助识别">
        </div>
        <div class="cs-form-row">
          <span class="cs-form-label">商品类别</span>
          <input class="cs-form-input" id="cs-ai-category" type="text" placeholder="可选，辅助识别">
        </div>
        <button type="button" class="cs-btn cs-btn-primary" id="cs-ai-btn" style="width:100%;margin-top:4px">
          AI识别并保存
        </button>
        <div id="cs-ai-result" class="cs-ai-result" style="display:none"></div>
        <div class="cs-status" id="cs-ai-status"></div>

        <!-- 设置（折叠） -->
        <div class="cs-ai-settings">
          <button type="button" class="cs-ai-settings-toggle" id="cs-ai-cfg-toggle">
            ⚙️ 设置
          </button>
          <div id="cs-ai-cfg-panel" style="display:none">
            <div class="cs-form-row" style="margin-top:8px">
              <span class="cs-form-label">API Key</span>
              <input class="cs-form-input" id="cs-cfg-key" type="password" placeholder="留空则不修改">
            </div>
            <div class="cs-form-row">
              <span class="cs-form-label">模型</span>
              <select class="cs-form-input" id="cs-cfg-model">
                <option value="qwen-vl-max">qwen-vl-max（稳定）</option>
                <option value="qwen-vl-max-latest">qwen-vl-max-latest</option>
                <option value="qwen3-vl-plus">qwen3-vl-plus（强）</option>
                <option value="qwen3-vl-flash">qwen3-vl-flash（快速）</option>
                <option value="qwen3.5-plus">qwen3.5-plus（最强）</option>
                <option value="qwen3.5-flash">qwen3.5-flash（快速）</option>
              </select>
            </div>
            <div class="cs-form-row">
              <span class="cs-form-label">Excel路径</span>
              <input class="cs-form-input" id="cs-cfg-excel" type="text" placeholder="留空则用 exe 同目录文件">
            </div>
            <div style="display:flex;gap:8px;margin-top:4px">
              <button type="button" class="cs-btn cs-btn-secondary" style="flex:1;height:32px;font-size:12px" id="cs-cfg-save">保存设置</button>
            </div>
            <div class="cs-status" id="cs-cfg-status" style="text-align:left"></div>
          </div>
        </div>
      </div>
    </div>`;

  return { toggleBtn, sidebar };
}

/* ═══════════════════════════════════════════════════════════════════
   UI：渲染商品列表
   ═══════════════════════════════════════════════════════════════════ */
function renderItems(items, rate, listEl, labelEl, totalEl, generateBtn) {
  generateBtn.disabled = !items.length;

  if (!items.length) {
    listEl.innerHTML    = '<div class="cs-no-data">未检测到商品行<br>请确认已打开调拨单详情页</div>';
    labelEl.textContent = '未读取到商品';
    totalEl.textContent = '$0.00';
    return;
  }

  rate = parseFloat(rate) || 7.2;
  var total = 0, html = '';

  items.forEach(function(item) {
    var usdPrice  = (item.rmb_price > 0 && rate > 0) ? (item.rmb_price / rate).toFixed(2) : null;
    var lineTotal = usdPrice ? (parseFloat(usdPrice) * item.qty).toFixed(2) : null;
    if (lineTotal) total += parseFloat(lineTotal);

    var priceHtml = item.rmb_price
      ? '<span style="color:#07c160;font-weight:600">¥' + item.rmb_price + (usdPrice ? ' → $' + usdPrice : '') + '</span>'
      : '<span class="cs-item-warn">⚠ 备注无价格</span>';
    var lineTotalHtml = (lineTotal && item.qty > 1)
      ? '<div style="color:#888;font-size:11px;margin-top:2px">× ' + item.qty + ' = $' + lineTotal + '</div>'
      : '';

    html += '<div class="cs-item-card">'
      + '<div style="flex:1;min-width:0">'
      + '<div class="cs-item-code">' + item.code + '</div>'
      + '<div class="cs-item-meta">' + item.qty + ' ' + (item.unit || '-') + '</div>'
      + '</div>'
      + '<div style="text-align:right;font-size:12px">' + priceHtml + lineTotalHtml + '</div>'
      + '</div>';
  });

  listEl.innerHTML    = html;
  labelEl.textContent = '检测到 ' + items.length + ' 件商品';
  totalEl.textContent = '$' + total.toFixed(2);
}

/* ═══════════════════════════════════════════════════════════════════
   UI：检查服务器状态
   ═══════════════════════════════════════════════════════════════════ */
function checkServer(dotEl) {
  serverFetch(SERVER + '/health', { timeout: 2500 })
    .then(function(data) {
      dotEl.className = 'cs-server-dot ' + (data.status === 'ok' ? 'online' : 'offline');
    })
    .catch(function() {
      dotEl.className = 'cs-server-dot offline';
    });
}

/* ═══════════════════════════════════════════════════════════════════
   UI：主初始化（只在顶层 frame 执行）
   ═══════════════════════════════════════════════════════════════════ */
function init() {
  if (document.getElementById('customs-toggle-btn')) return; // 防重复注入

  var ref     = buildSidebar();
  var toggleBtn = ref.toggleBtn;
  var sidebar   = ref.sidebar;
  document.body.appendChild(toggleBtn);
  document.body.appendChild(sidebar);

  var listEl      = document.getElementById('cs-items-list');
  var labelEl     = document.getElementById('cs-items-label');
  var totalEl     = document.getElementById('cs-total');
  var rateInput   = document.getElementById('cs-rate');
  var invoiceInput= document.getElementById('cs-invoice');
  var targetInput = document.getElementById('cs-target');
  var gwTotalInput= document.getElementById('cs-gw-total');
  var pathInput   = document.getElementById('cs-path');
  var statusEl    = document.getElementById('cs-status');
  var dotEl       = document.getElementById('cs-dot');
  var generateBtn = document.getElementById('cs-generate-btn');
  var refreshBtn  = document.getElementById('cs-refresh-btn');
  var browseBtn   = document.getElementById('cs-browse-btn');

  var currentItems = [];

  // 恢复保存的设置
  try {
    var s = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    if (s.rate)      rateInput.value    = s.rate;
    if (s.invoice)   invoiceInput.value = s.invoice;
    if (s.gw_total)  gwTotalInput.value = s.gw_total;
    if (s.path)      pathInput.value    = s.path;
  } catch(e) {}

  function saveSettings() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        rate: rateInput.value, invoice: invoiceInput.value,
        gw_total: gwTotalInput.value, path: pathInput.value,
      }));
    } catch(e) {}
  }

  function setStatus(html, cls) {
    statusEl.innerHTML  = html;
    statusEl.className  = 'cs-status ' + cls;
  }

  function bumpNo(no) {
    return no.replace(/(\d+)$/, function(m) {
      return String(Number(m) + 1).padStart(m.length, '0');
    });
  }

  function doRefresh() {
    setStatus('<span class="cs-spinner"></span>读取中...', 'info');
    var transferDoc = getTransferDoc();
    if (transferDoc) {
      var items = scrapeItems(transferDoc);
      if (items.length) {
        currentItems = items;
        renderItems(currentItems, rateInput.value, listEl, labelEl, totalEl, generateBtn);
        setStatus('', '');
        return;
      }
    }
    var requested = false;
    document.querySelectorAll('iframe').forEach(function(iframe) {
      try {
        iframe.contentWindow.postMessage({ type: 'CUSTOMS_REQUEST_SCRAPE' }, '*');
        requested = true;
      } catch(e) {}
    });
    if (!requested) {
      setStatus('未找到调拨单表格，请先打开一个调拨单', 'err');
      renderItems([], rateInput.value, listEl, labelEl, totalEl, generateBtn);
    } else {
      setTimeout(function() {
        if (!currentItems.length) setStatus('未读取到商品，请确认已打开调拨单详情', 'err');
      }, 2000);
    }
  }

  // 接收来自 iframe 的商品数据
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'CUSTOMS_ITEMS' && Array.isArray(e.data.items)) {
      currentItems = e.data.items;
      renderItems(currentItems, rateInput.value, listEl, labelEl, totalEl, generateBtn);
      setStatus('', '');
    }
  });

  // 开关侧边栏
  toggleBtn.addEventListener('click', function() {
    var opened = sidebar.classList.toggle('open');
    if (opened) { checkServer(dotEl); doRefresh(); }
  });

  // 关闭按钮
  document.getElementById('cs-close').addEventListener('click', function() {
    sidebar.classList.remove('open');
  });

  // 汇率变化时重新渲染
  rateInput.addEventListener('input', function() {
    renderItems(currentItems, rateInput.value, listEl, labelEl, totalEl, generateBtn);
    saveSettings();
  });

  invoiceInput.addEventListener('input', saveSettings);
  pathInput.addEventListener('input', saveSettings);
  refreshBtn.addEventListener('click', doRefresh);

  // 📁 选择文件夹
  browseBtn.addEventListener('click', function() {
    browseBtn.disabled    = true;
    browseBtn.textContent = '…';
    serverFetch(SERVER + '/browse-folder', { timeout: 120000 })
      .then(function(data) {
        if (data.success && data.path) {
          pathInput.value = data.path;
          saveSettings();
        } else if (data.error) {
          setStatus('❌ 选择失败：' + data.error, 'err');
        }
        // data.success=false 且无 error 表示用户取消，静默处理
      })
      .catch(function(e) {
        setStatus('❌ 无法打开文件夹选择器：' + (e.message || '请手动输入路径'), 'err');
      })
      .finally(function() {
        browseBtn.disabled    = false;
        browseBtn.textContent = '📁';
      });
  });

  // 生成文件
  generateBtn.addEventListener('click', function() {
    var rate    = parseFloat(rateInput.value);
    var invoice = invoiceInput.value.trim();
    var path    = pathInput.value.trim();

    if (!currentItems.length) { setStatus('❌ 请先刷新读取商品', 'err'); return; }
    if (!rate || rate <= 0)   { setStatus('❌ 请输入有效汇率', 'err'); return; }
    if (!invoice)              { setStatus('❌ 请输入发票号', 'err'); return; }
    if (!path)                 { setStatus('❌ 请输入保存路径', 'err'); return; }

    var noPriceCodes = currentItems.filter(function(i) { return !i.rmb_price; }).map(function(i) { return i.code; });
    if (noPriceCodes.length) {
      setStatus('⚠ 无价格：' + noPriceCodes.slice(0, 3).join(', ') + (noPriceCodes.length > 3 ? '…' : ''), 'err');
      return;
    }

    generateBtn.disabled = true;
    setStatus('<span class="cs-spinner"></span>生成中...', 'info');

    var payload = {
      items: currentItems.map(function(i) {
        return { code: i.code, unit: i.unit, qty: i.qty, rmb_price: i.rmb_price, remark: i.desc || '', boxes: i.boxes || 0 };
      }),
      exchange_rate:      rate,
      invoice_no:         invoice,
      target_total_usd:   parseFloat(targetInput.value) || null,
      weighed_total_gw:   parseFloat(gwTotalInput.value) || null,
      output_path:        path,
    };

    serverFetch(SERVER + '/generate', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(payload),
      timeout: 30000,
    })
      .then(function(data) {
        if (data.success) {
          var msg = '✅ 生成成功！合计 $' + data.total_usd;
          if (data.not_found && data.not_found.length) msg += '  ⚠ 未找到：' + data.not_found.join(', ');
          setStatus(msg, 'ok');
          invoiceInput.value = bumpNo(invoice);
          saveSettings();
        } else {
          setStatus('❌ ' + (data.error || '生成失败'), 'err');
        }
      })
      .catch(function(e) {
        setStatus('❌ ' + (e.message || '生成失败'), 'err');
      })
      .finally(function() {
        generateBtn.disabled = false;
      });
  });

  // ── 切换到 AI Tab 时加载当前配置 ──────────────────────────────────
  function loadAiConfig() {
    serverFetch(SERVER + '/ai-config')
      .then(function(data) {
        if (!data.success) return;
        var modelEl = document.getElementById('cs-cfg-model');
        if (data.qianwen_model) modelEl.value = data.qianwen_model;
        var excelEl = document.getElementById('cs-cfg-excel');
        if (data.excel_path) excelEl.value = data.excel_path;
        // Key 有值则在 placeholder 显示打码
        var keyEl = document.getElementById('cs-cfg-key');
        keyEl.placeholder = data.has_key ? '已配置：' + data.qianwen_api_key_masked : '未配置，请填入 Key';
      })
      .catch(function() {});
  }

  // ── Tab 切换 ──────────────────────────────────────────────────────
  document.getElementById('cs-tab-gen').addEventListener('click', function() {
    document.getElementById('cs-tab-gen').classList.add('active');
    document.getElementById('cs-tab-ai').classList.remove('active');
    document.getElementById('cs-panel-gen').classList.add('cs-tab-panel--active');
    document.getElementById('cs-panel-ai').classList.remove('cs-tab-panel--active');
  });
  document.getElementById('cs-tab-ai').addEventListener('click', function() {
    document.getElementById('cs-tab-ai').classList.add('active');
    document.getElementById('cs-tab-gen').classList.remove('active');
    document.getElementById('cs-panel-ai').classList.add('cs-tab-panel--active');
    document.getElementById('cs-panel-gen').classList.remove('cs-tab-panel--active');
    loadAiConfig();
  });

  // ── 批量读取页面商品 ──────────────────────────────────────────────
  var batchItems = [];

  function renderBatchList() {
    var listEl  = document.getElementById('cs-batch-list');
    var countEl = document.getElementById('cs-batch-count');
    countEl.textContent = batchItems.length;
    listEl.innerHTML = batchItems.map(function(item, idx) {
      return '<div class="cs-batch-item" id="bi-' + idx + '">'
        + '<img src="' + item.imgUrl + '" class="cs-batch-thumb" onerror="this.style.display=\'none\'">'
        + '<div style="flex:1;min-width:0">'
        +   '<div class="cs-item-code" style="font-size:11px">' + item.code + '</div>'
        +   '<div class="cs-batch-result" id="br-' + idx + '" style="font-size:11px;color:#aaa">待识别</div>'
        + '</div>'
        + '<button class="cs-btn cs-btn-secondary cs-batch-single" data-idx="' + idx + '"'
        +   ' style="height:26px;font-size:11px;padding:0 8px;flex:none">识别</button>'
        + '</div>';
    }).join('');

    // 单条识别按钮
    listEl.querySelectorAll('.cs-batch-single').forEach(function(btn) {
      btn.addEventListener('click', function() {
        var idx = parseInt(this.getAttribute('data-idx'));
        runIdentify(idx);
      });
    });
  }

  function runIdentify(idx) {
    return new Promise(function(resolve) {
      var item    = batchItems[idx];
      var resultEl = document.getElementById('br-' + idx);
      var btn     = document.querySelector('[data-idx="' + idx + '"]');
      if (!item || !resultEl) { resolve(); return; }

      if (btn) btn.disabled = true;
      resultEl.innerHTML = '<span class="cs-spinner"></span>';
      resultEl.style.color = '#888';

      serverFetch(SERVER + '/ai-identify', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          code:      item.code,
          name:      item.name,
          category:  item.category,
          image_url: item.imgUrl,
        }),
        timeout: 90000,
      })
        .then(function(data) {
          if (data.success) {
            if (data.skipped) {
              resultEl.style.color = '#07c160';
              resultEl.textContent = (data.material || '已识别') + (data.hs_code ? ' (' + data.hs_code + ')' : '') + ' [跳过]';
            } else if (data.matched) {
              resultEl.style.color = '#07c160';
              resultEl.textContent = data.material + ' (' + data.hs_code + ')';
            } else {
              resultEl.style.color = '#e67e22';
              resultEl.textContent = '未匹配：' + data.ai_text;
            }
          } else {
            resultEl.style.color = '#e74c3c';
            resultEl.textContent = '失败：' + (data.error || '');
          }
        })
        .catch(function(e) {
          resultEl.style.color = '#e74c3c';
          resultEl.textContent = '错误：' + (e.message || '');
        })
        .finally(function() {
          if (btn) btn.disabled = false;
          resolve();
        });
    });
  }

  document.getElementById('cs-ai-scrape').addEventListener('click', function() {
    batchItems = scrapePurchaseImages();
    var wrap = document.getElementById('cs-batch-wrap');
    if (!batchItems.length) {
      wrap.style.display = 'none';
      setAiStatus('未找到带图片的商品行，请确认已打开采购订单', 'err');
      return;
    }
    wrap.style.display = 'block';
    setAiStatus('', '');
    renderBatchList();
  });

  document.getElementById('cs-batch-all').addEventListener('click', function() {
    var self = this;
    self.disabled = true;
    setAiStatus('<span class="cs-spinner"></span>正在检查已识别记录...', 'info');

    // 第一步：批量查 Excel，已有的立刻标跳过
    serverFetch(SERVER + '/ai-codes', { timeout: 15000 })
      .then(function(data) {
        var existingSet = {};
        if (data.success && data.codes) {
          data.codes.forEach(function(c) { existingSet[c] = true; });
        }
        // 标记已有编码
        var needAI = 0;
        batchItems.forEach(function(item, i) {
          if (existingSet[item.code]) {
            var resultEl = document.getElementById('br-' + i);
            if (resultEl && resultEl.style.color !== 'rgb(7, 193, 96)') {
              resultEl.style.color = '#07c160';
              resultEl.textContent = '[已识别，跳过]';
            }
          } else {
            needAI++;
          }
        });
        setAiStatus('需要识别 ' + needAI + ' 件，开始调用 AI...', 'info');

        // 第二步：只对未识别的逐条发 AI
        var idx = 0;
        function next() {
          if (idx >= batchItems.length) {
            self.disabled = false;
            setAiStatus('全部完成', 'ok');
            return;
          }
          var resultEl = document.getElementById('br-' + idx);
          var done = resultEl && resultEl.style.color === 'rgb(7, 193, 96)';
          if (done) { idx++; setTimeout(next, 50); return; }
          var currentIdx = idx++;
          runIdentify(currentIdx).then(function() { setTimeout(next, 1500); });
        }
        next();
      })
      .catch(function() {
        // 查询失败就直接跑
        setAiStatus('', '');
        var idx = 0;
        function next() {
          if (idx >= batchItems.length) { self.disabled = false; return; }
          var resultEl = document.getElementById('br-' + idx);
          var done = resultEl && resultEl.style.color === 'rgb(7, 193, 96)';
          if (done) { idx++; setTimeout(next, 50); return; }
          var currentIdx = idx++;
          runIdentify(currentIdx).then(function() { setTimeout(next, 1500); });
        }
        next();
      });
  });

  // ── 设置面板折叠 / 保存 ───────────────────────────────────────────
  document.getElementById('cs-ai-cfg-toggle').addEventListener('click', function() {
    var panel = document.getElementById('cs-ai-cfg-panel');
    var open  = panel.style.display === 'none';
    panel.style.display = open ? 'block' : 'none';
    this.textContent    = open ? '⚙️ 设置 ▲' : '⚙️ 设置';
  });

  document.getElementById('cs-cfg-save').addEventListener('click', function() {
    var cfgStatus = document.getElementById('cs-cfg-status');
    cfgStatus.className = 'cs-status info';
    cfgStatus.textContent = '保存中...';

    serverFetch(SERVER + '/ai-config', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        qianwen_api_key: document.getElementById('cs-cfg-key').value.trim(),
        qianwen_model:   document.getElementById('cs-cfg-model').value,
        excel_path:      document.getElementById('cs-cfg-excel').value.trim(),
      }),
      timeout: 5000,
    })
      .then(function(data) {
        if (data.success) {
          cfgStatus.className   = 'cs-status ok';
          cfgStatus.textContent = '已保存';
          document.getElementById('cs-cfg-key').value = '';
          loadAiConfig();
        } else {
          cfgStatus.className   = 'cs-status err';
          cfgStatus.textContent = '保存失败：' + (data.error || '');
        }
      })
      .catch(function(e) {
        cfgStatus.className   = 'cs-status err';
        cfgStatus.textContent = '请求失败：' + (e.message || '');
      });
  });

  // ── AI 识别 ───────────────────────────────────────────────────────
  var aiBtn      = document.getElementById('cs-ai-btn');
  var aiStatus   = document.getElementById('cs-ai-status');
  var aiResult   = document.getElementById('cs-ai-result');

  function setAiStatus(html, cls) {
    aiStatus.innerHTML  = html;
    aiStatus.className  = 'cs-status ' + (cls || '');
  }

  aiBtn.addEventListener('click', function() {
    var url      = (document.getElementById('cs-ai-url').value      || '').trim();
    var code     = (document.getElementById('cs-ai-code').value     || '').trim();
    var name     = (document.getElementById('cs-ai-name').value     || '').trim();
    var category = (document.getElementById('cs-ai-category').value || '').trim();

    if (!code) { setAiStatus('请填写商品编码', 'err'); return; }
    if (!url)  { setAiStatus('请填写图片 URL', 'err'); return; }

    aiBtn.disabled  = true;
    aiResult.style.display = 'none';
    setAiStatus('<span class="cs-spinner"></span>AI 识别中...', 'info');

    serverFetch(SERVER + '/ai-identify', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ code: code, name: name, category: category, image_url: url }),
      timeout: 90000,
    })
      .then(function(data) {
        if (data.success) {
          var matchedHtml = data.matched
            ? '<span class="ai-val">' + (data.material || '') + '</span>'
              + '&nbsp;&nbsp;<span class="ai-label">HS:</span> '
              + '<span class="ai-val">' + (data.hs_code || '') + '</span>'
            : '<span class="ai-warn">未匹配预设分类，已写入备注列</span>';
          aiResult.innerHTML =
            '<div><span class="ai-label">编码：</span>' + data.code + '</div>'
            + '<div><span class="ai-label">AI原文：</span>' + data.ai_text + '</div>'
            + '<div><span class="ai-label">材料：</span>' + matchedHtml + '</div>'
            + '<div><span class="ai-label">写入行：</span>第 ' + data.row + ' 行</div>';
          aiResult.style.display = 'block';
          setAiStatus('', '');
        } else {
          setAiStatus('识别失败：' + (data.error || '未知错误'), 'err');
        }
      })
      .catch(function(e) {
        setAiStatus('请求失败：' + (e.message || ''), 'err');
      })
      .finally(function() {
        aiBtn.disabled = false;
      });
  });

  checkServer(dotEl);
}

/* ═══════════════════════════════════════════════════════════════════
   入口：根据 frame 类型分支执行
   ═══════════════════════════════════════════════════════════════════ */
if (IS_IFRAME_TRANSFER) {
  // 子 iframe：只做数据采集，响应 postMessage
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'CUSTOMS_REQUEST_SCRAPE') {
      window.parent.postMessage({ type: 'CUSTOMS_ITEMS', items: scrapeItems(document) }, '*');
    }
  });
  function pushItems() {
    var items = scrapeItems(document);
    if (items.length) window.parent.postMessage({ type: 'CUSTOMS_ITEMS', items: items }, '*');
  }
  if (document.readyState === 'complete') {
    setTimeout(pushItems, 600);
  } else {
    window.addEventListener('load', function() { setTimeout(pushItems, 600); });
  }

} else {
  // 顶层 frame：注入侧边栏 UI
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // 监听 SPA hash 路由变化
  var _lastHref = location.href;
  new MutationObserver(function() {
    if (location.href === _lastHref) return;
    _lastHref = location.href;
    setTimeout(function() {
      if (!document.getElementById('customs-toggle-btn')) init();
    }, 1000);
  }).observe(document.documentElement, { childList: true, subtree: true });
}
