// 采购工程流程前端（角色：采购方 / 供应商）
// 下单：多行产品表格（1-50 行），型号自动匹配历史账单回填参数/尺寸/材料/单价
const PO_LABELS = { pending_confirm: '待确认', confirmed: '已接单', received: '已收货', rejected: '已拒接' };
const PO_CLS = { pending_confirm: 'po-st-pending', confirmed: 'po-st-confirmed',
                 received: 'po-st-received', rejected: 'po-st-rejected' };
const PO_MAX_LINES = 50;
// 图纸/凭证允许格式：唯一来源是后端 po_db.DOC_EXTS（由模板注入），前端不再各写一份
const PO_DOC_EXTS = (window.PO_DOC_EXTS && window.PO_DOC_EXTS.length)
  ? window.PO_DOC_EXTS
  : ['.pdf', '.dwg', '.dxf', '.png', '.jpg', '.jpeg', '.xlsx', '.xls'];
const PO_ACCEPT = window.PO_DOC_ACCEPT || PO_DOC_EXTS.join(',');
let poState = { user: null, filter: '', current: null };
let poLines = [];   // 下单行：[{model,param,size,material,quantity,unit,unit_price,required_date,remark,drawingFile,match:{state,msg}}]

function poBadge(st) {
  return `<span class="po-badge ${PO_CLS[st] || ''}">${PO_LABELS[st] || esc(st)}</span>`;
}
function kv(k, v) {
  const val = (v === null || v === undefined || v === '') ? '—' : v;
  return `<div class="po-kv-item"><span>${k}</span><b>${val}</b></div>`;
}
function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function blankLine() {
  return { model: '', param: '', size: '', material: '', quantity: '', unit: '',
           unit_price: '', required_date: '', remark: '', drawingFile: null,
           match: { state: 'empty', msg: '' }, _auto: {} };
}

// 自动填充字段的来源标注：浅蓝底 + 悬停显示「来源：账期·供应商·文件名 第N行」
const PO_AUTO_FIELDS = ['param', 'size', 'material', 'unit_price'];
const PO_AUTO_NAMES = { param: '参数', size: '尺寸', material: '材料', unit_price: '单价' };

function autoMeta(l, f) {
  const a = (l && l._auto) || {};
  return a[f] || null;
}
function autoCls(l, f) {
  return autoMeta(l, f) ? ' po-auto' : '';
}
function autoTip(l, f) {
  const m = autoMeta(l, f);
  if (!m) return '';
  const src = (m.source && m.source.text) || '历史账单';
  return ` title="自动填充·来源：${esc(src)}"`;
}

// ---------- 登录 / 身份 ----------
function poShowLogin() {
  $('#poLoginCard').style.display = '';
  $('#poApp').style.display = 'none';
}
function poShowApp() {
  const u = poState.user;
  const isPurchaser = u.role === 'purchaser';
  $('#poLoginCard').style.display = 'none';
  $('#poApp').style.display = '';
  $('#poRoleBadge').textContent = isPurchaser ? '采购方' : '供应商';
  $('#poRoleBadge').className = 'badge ' + (isPurchaser ? 'ok' : '');
  $('#poWho').textContent = (u.display_name || u.username) + (u.supplier ? ' · ' + u.supplier : '');
  $('#poCreateCard').style.display = isPurchaser ? '' : 'none';
  $('#poListTitle').textContent = isPurchaser ? '全部订单' : `我的订单（${u.supplier || ''}）`;
  $('#poListHint').textContent = isPurchaser
    ? '点击订单行查看详情与产品明细；「已接单」的订单可登记收货'
    : '点击订单行查看详情、下载各行图纸并确认接单';
  if (isPurchaser && poLines.length === 0) { poLines = [blankLine()]; renderEntry(); }
  poLoadSuppliers();
}

function poRenderStats(st) {
  if (!st) return;
  $('#poStats').innerHTML = ['pending_confirm', 'confirmed', 'received', 'rejected']
    .map(k => `<span class="po-stat">${PO_LABELS[k]} <b>${st[k] || 0}</b></span>`).join('');
}

async function poRefreshMe() {
  try {
    const r = await fetch('/api/po/me').then(x => x.json());
    if (!r.ok) { poState.user = null; poShowLogin(); return; }
    poState.user = r.user;
    poShowApp(); poRenderStats(r.stats); poLoadOrders();
  } catch (e) { poState.user = null; poShowLogin(); }
}

// 供应商下拉与后端匹配口径保持一致（内置供应商 ∪ 历史库实际出现的供应商）
async function poLoadSuppliers() {
  try {
    const r = await fetch('/api/po/suppliers').then(x => x.json());
    if (!r.ok || !r.suppliers || !r.suppliers.length) return;
    const sel = $('#poSupplier');
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">请选择</option>'
      + r.suppliers.map(s => `<option>${esc(s)}</option>`).join('');
    if (cur && r.suppliers.includes(cur)) sel.value = cur;
  } catch (e) { /* 供应商列表取不到时沿用模板内置选项 */ }
}

// ---------- 订单列表 ----------
async function poLoadOrders() {
  if (!poState.user) return;
  const q = poState.filter ? '?status=' + poState.filter : '';
  try {
    const r = await fetch('/api/po/orders' + q).then(x => x.json());
    if (!r.ok) { $('#poOrders').innerHTML = '<div class="empty">加载失败</div>'; return; }
    poRenderStats(r.stats);
    const el = $('#poOrders');
    if (!r.orders.length) { el.innerHTML = '<div class="empty">暂无订单</div>'; return; }
    let h = '<table><thead><tr><th>订单号</th><th>供应商</th><th>产品</th><th>总数量</th>'
          + '<th>金额</th><th>状态</th><th>下单时间</th><th></th></tr></thead><tbody>';
    r.orders.forEach(o => {
      h += `<tr><td>${esc(o.order_no)}</td><td>${esc(o.supplier)}</td>
        <td>${esc(o.product_name)}</td>
        <td>${esc(o.quantity)}</td>
        <td>${o.total_amount != null ? '¥' + o.total_amount : '—'}</td>
        <td>${poBadge(o.status)}</td>
        <td class="muted">${esc(o.created_at || '')}</td>
        <td><button class="btn ghost small" data-detail="${esc(o.order_no)}">详情</button></td></tr>`;
    });
    el.innerHTML = h + '</tbody></table>';
    el.querySelectorAll('[data-detail]').forEach(b => b.onclick = () => poOpenDetail(b.dataset.detail));
  } catch (e) { $('#poOrders').innerHTML = '<div class="empty">加载失败，请刷新</div>'; }
}

// ---------- 订单详情（含产品明细 + 行级图纸） ----------
async function poOpenDetail(no) {
  try {
    const r = await fetch('/api/po/orders/' + no).then(x => x.json());
    if (!r.ok) { toast(r.error || '加载失败'); return; }
    poState.current = r;
    const o = r.order;
    const items = r.items || [];
    $('#poDetailTitle').textContent = '订单详情 · ' + o.order_no;

    let h = '<div class="po-kv">'
      + kv('订单号', esc(o.order_no)) + kv('状态', poBadge(o.status))
      + kv('供应商', esc(o.supplier)) + kv('采购员', esc(o.purchaser))
      + kv('产品项数', items.length ? (items.length + ' 行') : '1 行')
      + kv('总数量', esc(o.quantity))
      + kv('总金额', o.total_amount != null ? '¥' + o.total_amount : '')
      + kv('最早交期', esc(o.required_date || ''))
      + kv('下单时间', esc(o.created_at || ''))
      + kv('确认时间', esc(o.confirmed_at || ''))
      + kv('确认人', esc(o.confirmed_by || ''))
      + (o.remark ? kv('整单备注', esc(o.remark)) : '')
      + '</div>';

    if (items.length) {
      h += `<h4 class="po-tl-title">产品明细（${items.length} 行）</h4>
        <div class="po-items-wrap"><table class="po-items-table"><thead><tr>
          <th>行</th><th>型号</th><th>参数</th><th>尺寸</th><th>材料</th>
          <th>数量</th><th>单价</th><th>交货日期</th><th>特殊要求</th><th>图纸</th>
        </tr></thead><tbody>`;
      items.forEach(it => {
        const dl = it.drawing_name
          ? `<a class="po-file" href="/api/po/orders/${esc(o.order_no)}/file?type=drawing&line=${it.line_no}">📎 ${esc(it.drawing_name)}</a>`
          : '<span class="muted">无</span>';
        h += `<tr><td>${it.line_no}</td><td>${esc(it.model || '')}</td>
          <td class="muted">${esc(it.param || '—')}</td>
          <td>${esc(it.size || '—')}</td><td>${esc(it.material || '—')}</td>
          <td>${esc(it.quantity)}${esc(it.unit || '')}</td>
          <td>${it.unit_price != null ? '¥' + it.unit_price : '—'}</td>
          <td>${esc(it.required_date || '—')}</td>
          <td class="cell-reason">${esc(it.remark || '—')}</td>
          <td>${dl}</td></tr>`;
      });
      h += '</tbody></table></div>';
    }

    // 订单级附件（旧单行订单兼容 + 收货凭证）
    h += '<div class="po-files">附件：';
    if (!items.length && o.drawing_name) {
      h += `<a class="po-file" href="/api/po/orders/${esc(o.order_no)}/file?type=drawing">📎 图纸 · ${esc(o.drawing_name)}</a>`;
    }
    if (o.receipt_name) {
      h += `<a class="po-file" href="/api/po/orders/${esc(o.order_no)}/file?type=receipt">📎 收货凭证 · ${esc(o.receipt_name)}</a>`;
    }
    if ((items.length && items.every(x => !x.drawing_name)) && !o.receipt_name) {
      h += '<span class="muted">暂无附件</span>';
    }
    h += '</div>';

    if (o.status === 'rejected') h += `<div class="po-reject-note">拒接原因：${esc(o.reject_reason || '—')}</div>`;
    if (o.status === 'received') {
      h += '<div class="po-receive-info"><h4>收货信息</h4><div class="po-kv">'
        + kv('到货数量', esc(o.received_qty)) + kv('到货日期', esc(o.received_date || ''))
        + kv('收货时间', esc(o.received_at || ''))
        + (o.receive_remark ? kv('收货备注', esc(o.receive_remark)) : '')
        + '</div></div>';
    }

    h += '<h4 class="po-tl-title">进度轨迹</h4><ul class="po-timeline">';
    r.events.forEach(e => {
      h += `<li><div class="po-tl-dot"></div><div>
        <div class="po-tl-detail">${esc(e.detail)}</div>
        <div class="po-tl-meta">${esc(e.created_at)} · ${esc(e.actor || '')}</div></div></li>`;
    });
    h += '</ul>';

    if (r.actions.includes('receive')) {
      h += `<div class="po-receive-form"><h4>登记收货</h4>
        <div class="form-row"><label>到货数量 <span class="req">*</span></label>
          <input id="poRcvQty" type="number" step="any" min="0.001" value="${esc(o.quantity)}"></div>
        <div class="form-row"><label>到货日期 <span class="req">*</span></label>
          <input id="poRcvDate" type="date"></div>
        <div class="form-row"><label>收货备注</label>
          <input id="poRcvRemark" placeholder="可选，如包装/外观情况"></div>
        <div class="form-row"><label>收货凭证</label>
          <input id="poRcvFile" type="file" accept="${PO_ACCEPT}"></div>
        <div class="actions"><button class="btn primary" id="btnPoReceive">提交收货</button></div>
        <div class="msg" id="poRcvMsg"></div></div>`;
    }

    $('#poDetailBody').innerHTML = h;
    const dEl = $('#poRcvDate');
    if (dEl) dEl.value = todayIso();
    const rcvBtn = $('#btnPoReceive');
    if (rcvBtn) rcvBtn.onclick = () => poReceive(o.order_no);

    $('#btnPoConfirm').style.display = r.actions.includes('confirm') ? '' : 'none';
    $('#btnPoReject').style.display = r.actions.includes('reject') ? '' : 'none';
    $('#btnPoConfirm').onclick = () => poConfirm(o.order_no);
    $('#btnPoReject').onclick = () => poReject(o.order_no);
    $('#poDetailModal').classList.add('show');
  } catch (e) { toast('详情加载失败：' + e.message); }
}

// ---------- 确认 / 拒接 / 收货 ----------
async function poConfirm(no) {
  if (!confirm('确认接单？确认后订单将进入「已接单」，等待采购方收货。')) return;
  try {
    const r = await fetch('/api/po/orders/' + no + '/confirm', { method: 'POST' }).then(x => x.json());
    if (!r.ok) { toast(r.error || '操作失败'); return; }
    toast(r.msg || '已确认接单');
    $('#poDetailModal').classList.remove('show');
    poLoadOrders();
  } catch (e) { toast('请求失败：' + e.message); }
}

async function poReject(no) {
  const reason = prompt('请填写拒接原因（可留空）：', '');
  if (reason === null) return;
  try {
    const r = await fetch('/api/po/orders/' + no + '/reject', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason })
    }).then(x => x.json());
    if (!r.ok) { toast(r.error || '操作失败'); return; }
    toast(r.msg || '已拒接订单');
    $('#poDetailModal').classList.remove('show');
    poLoadOrders();
  } catch (e) { toast('请求失败：' + e.message); }
}

async function poReceive(no) {
  const msg = $('#poRcvMsg');
  msg.className = 'msg'; msg.textContent = '';
  const qty = $('#poRcvQty').value.trim();
  const date = $('#poRcvDate').value.trim();
  if (!qty || Number(qty) <= 0) { msg.className = 'msg error'; msg.textContent = '到货数量必须大于 0'; return; }
  if (!date) { msg.className = 'msg error'; msg.textContent = '请选择到货日期'; return; }
  const fd = new FormData();
  fd.append('received_qty', qty);
  fd.append('received_date', date);
  fd.append('receive_remark', $('#poRcvRemark').value.trim());
  const f = $('#poRcvFile').files[0];
  if (f) fd.append('receipt', f);
  const btn = $('#btnPoReceive');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const r = await fetch('/api/po/orders/' + no + '/receive', { method: 'POST', body: fd }).then(x => x.json());
    if (!r.ok) {
      msg.className = 'msg error';
      msg.textContent = (r.error || '收货登记失败') + (r.errors ? '：' + r.errors.join('；') : '');
      return;
    }
    toast(r.msg || '收货登记成功');
    $('#poDetailModal').classList.remove('show');
    poLoadOrders();
  } catch (e) { msg.className = 'msg error'; msg.textContent = '请求失败：' + e.message; }
  finally { btn.disabled = false; btn.textContent = '提交收货'; }
}

// ---------- 多行下单表单 ----------
function matchShort(m) {
  const map = { ok: '✓ 已匹配', picked: '✓ 已选定', near: '≈ 相近型号', warn: '⚠ 需确认',
                miss: '✕ 未找到', err: '✕ 查询失败', loading: '查询中…', empty: '—' };
  return map[m && m.state] || '—';
}
function matchClass(m) {
  return { ok: 'po-ok', picked: 'po-ok', near: 'po-warn', warn: 'po-warn', miss: 'po-miss',
           err: 'po-miss', loading: 'po-loading' }[(m && m.state)] || '';
}

// 是否需要「选择」：多候选、相近型号、或字段不唯一时为 true；完全确定（ok）则不打扰
function needPick(l) {
  const m = (l && l.match) || {};
  const c = m.candidates || [];
  if (!c.length) return false;
  if (c.length > 1) return true;
  return m.state !== 'ok';
}
function pickLabel(l) {
  const m = (l && l.match) || {};
  const n = (m.candidates || []).length;
  return m.state === 'picked' ? '改选' : `选择 ${n}`;
}

function rowHtml(l, i) {
  const fileCell = l.drawingFile
    ? `<span class="po-file-name" title="${esc(l.drawingFile.name)}">📎 ${esc(l.drawingFile.name)}</span>
       <span class="po-file-x" data-clear="${i}" title="移除该行图纸">✕</span>`
    : `<input type="file" class="po-file-input" data-file="${i}" accept="${PO_ACCEPT}">`;
  const price = (l.unit_price === null || l.unit_price === undefined) ? '' : l.unit_price;
  const cands = (l.match && l.match.candidates) || [];
  const pickBtn = needPick(l)
    ? `<button class="po-pick" data-pick="${i}" title="历史账单中有 ${cands.length} 条记录，点此查看并选用">${pickLabel(l)}</button>`
    : '';
  return `<tr data-idx="${i}">
    <td class="c-line">${i + 1}</td>
    <td><input class="po-cell-input po-cell-model" data-idx="${i}" data-field="model" value="${esc(l.model || '')}" placeholder="型号"></td>
    <td class="c-match"><span class="po-match ${matchClass(l.match)}" title="${esc((l.match && l.match.msg) || '')}">${matchShort(l.match)}</span>${pickBtn}</td>
    <td><input class="po-cell-input${autoCls(l, 'param')}" data-idx="${i}" data-field="param" value="${esc(l.param || '')}" placeholder="自动"${autoTip(l, 'param')}></td>
    <td><input class="po-cell-input${autoCls(l, 'size')}" data-idx="${i}" data-field="size" value="${esc(l.size || '')}" placeholder="自动"${autoTip(l, 'size')}></td>
    <td><input class="po-cell-input${autoCls(l, 'material')}" data-idx="${i}" data-field="material" value="${esc(l.material || '')}" placeholder="自动"${autoTip(l, 'material')}></td>
    <td><input class="po-cell-input num" type="number" min="0.001" step="any" data-idx="${i}" data-field="quantity" value="${esc(l.quantity || '')}" placeholder="数量"></td>
    <td><input class="po-cell-input narrow" data-idx="${i}" data-field="unit" value="${esc(l.unit || '')}" placeholder="件"></td>
    <td><input class="po-cell-input num${autoCls(l, 'unit_price')}" type="number" min="0" step="any" data-idx="${i}" data-field="unit_price" value="${esc(price)}" placeholder="参考"${autoTip(l, 'unit_price')}></td>
    <td><input class="po-cell-input" type="date" data-idx="${i}" data-field="required_date" value="${esc(l.required_date || '')}"></td>
    <td><input class="po-cell-input" data-idx="${i}" data-field="remark" value="${esc(l.remark || '')}" placeholder="特殊要求"></td>
    <td class="c-file">${fileCell}</td>
    <td class="c-del"><button class="po-row-del" data-del="${i}" title="删除该行">✕</button></td>
  </tr>`;
}

function renderEntry() {
  $('#poEntryBody').innerHTML = poLines.map((l, i) => rowHtml(l, i)).join('');
  $('#poLineCount').textContent = `已添加 ${poLines.length} / ${PO_MAX_LINES} 行`;
  $('#btnPoAddLine').disabled = poLines.length >= PO_MAX_LINES;
  renderMatchSummary();
}

function updateMatchCell(idx) {
  const tr = document.querySelector(`#poEntryBody tr[data-idx="${idx}"]`);
  if (!tr) return;
  const cell = tr.querySelector('.c-match');
  const span = tr.querySelector('.po-match');
  if (!span || !cell) return;
  const m = poLines[idx].match || { state: 'empty', msg: '' };
  span.className = 'po-match ' + matchClass(m);
  span.textContent = matchShort(m);
  span.title = m.msg || '';
  // 多条候选 → 显示「选择 N」按钮；唯一候选 → 隐藏
  const cands = m.candidates || [];
  let btn = cell.querySelector('.po-pick');
  if (needPick(poLines[idx])) {
    if (!btn) {
      btn = document.createElement('button');
      btn.className = 'po-pick';
      btn.type = 'button';
      cell.appendChild(btn);
    }
    btn.dataset.pick = idx;
    btn.textContent = pickLabel(poLines[idx]);
    btn.title = `历史账单中有 ${cands.length} 条记录，点此查看并选用`;
  } else if (btn) {
    btn.remove();
  }
}

function fillRowInputs(idx) {
  const tr = document.querySelector(`#poEntryBody tr[data-idx="${idx}"]`);
  if (!tr) return;
  const l = poLines[idx];
  PO_AUTO_FIELDS.forEach(f => {
    const el = tr.querySelector(`[data-field="${f}"]`);
    if (!el) return;
    el.value = (l[f] === null || l[f] === undefined) ? '' : l[f];
    const meta = autoMeta(l, f);
    el.classList.toggle('po-auto', !!meta);
    if (meta) el.title = `自动填充·来源：${(meta.source && meta.source.text) || '历史账单'}`;
    else el.removeAttribute('title');
  });
}

// 把一条候选/自动填充值写入行状态，并记录来源
function applyField(idx, field, value, source) {
  const l = poLines[idx];
  if (!l) return;
  l[field] = (value === null || value === undefined) ? '' : value;
  if (!l._auto) l._auto = {};
  if (value === null || value === undefined || value === '') delete l._auto[field];
  else l._auto[field] = { value: value, source: source || { text: '历史账单' } };
}

// 撤掉该行所有「自动填充」的字段值（型号改动/匹配失效时使用），人工填写的值不动
function clearAutoFields(idx) {
  const l = poLines[idx];
  if (!l || !l._auto) return;
  Object.keys(l._auto).forEach(f => {
    if (PO_AUTO_FIELDS.includes(f)) { l[f] = ''; delete l._auto[f]; }
  });
  const tr = document.querySelector(`#poEntryBody tr[data-idx="${idx}"]`);
  if (tr) {
    PO_AUTO_FIELDS.forEach(f => {
      const el = tr.querySelector(`[data-field="${f}"]`);
      if (el) {
        el.value = '';
        el.classList.remove('po-auto');
        el.removeAttribute('title');
      }
    });
  }
}

function renderMatchSummary() {
  const miss = [], warn = [], near = [], autoLines = [];
  poLines.forEach((l, i) => {
    if (!(l.model || '').trim()) return;
    const st = l.match && l.match.state;
    if (st === 'miss') miss.push(i + 1);
    else if (st === 'near') near.push(i + 1);
    else if (st === 'warn' && l.match && l.match.risk) warn.push(i + 1);
    // 自动填充来源逐行列出，便于复核「这个尺寸是从哪张账单来的」
    const a = l._auto || {};
    const filled = PO_AUTO_FIELDS.filter(f => a[f]);
    if (filled.length) {
      const src = (a[filled[0]].source && a[filled[0]].source.text) || '历史账单';
      const desc = filled.map(f => `${PO_AUTO_NAMES[f]} ${a[f].value}`).join(' / ');
      autoLines.push(`<li>第 ${i + 1} 行：${esc(desc)} ← 来源：${esc(src)}</li>`);
    }
  });
  const el = $('#poMatchSummary');
  if (!el) return;
  let h = '';
  if (autoLines.length) {
    h += `<div class="po-note auto"><div class="po-note-title">自动填充字段及数据来源（历史账单）</div>
      <ul class="po-auto-list">${autoLines.join('')}</ul></div>`;
  }
  if (miss.length) {
    h += `<div class="po-note warn">✕ 第 ${miss.join('、')} 行未在历史账单中找到相同或相近型号：请手工填写尺寸/材质，或核对型号写法与供应商选择。</div>`;
  }
  if (near.length) {
    const detail = near.map(i => {
      const m = poLines[i - 1].match || {};
      return `第 ${i} 行 → ${m.matched || '相近型号'}`;
    }).join('；');
    h += `<div class="po-note warn">≈ 第 ${near.join('、')} 行为相近型号（未自动填充，需人工选定）：${esc(detail)}，点「选择」查看候选记录。</div>`;
  }
  if (warn.length) {
    h += `<div class="po-note warn">⚠ 第 ${warn.join('、')} 行同型号存在多条不同记录（尺寸/材质/单价不唯一）：已列出候选，请点「选择」确定后再提交。</div>`;
  }
  el.innerHTML = h;
}

// ---------- 候选选择弹窗（历史账单多条命中时由人工定夺） ----------
function poOpenPicker(idx) {
  const l = poLines[idx];
  if (!l) return;
  const m = l.match || {};
  const cands = m.candidates || [];
  if (!cands.length) return;
  poState.pickIdx = idx;
  $('#poPickTitle').textContent = `第 ${idx + 1} 行 · 选择历史账单匹配项（${cands.length} 条）`;
  $('#poPickHint').textContent = (m.matchLabel ? `匹配方式：${m.matchLabel}｜` : '')
    + (m.msg || '请选择与实际需求一致的记录，选定后自动填充尺寸/材质/单价并标注来源。');
  let h = '<div class="po-items-wrap"><table class="po-items-table po-cand-table"><thead><tr>'
    + '<th>#</th><th>历史型号</th><th>尺寸</th><th>材质</th><th>单价</th>'
    + '<th>账期</th><th>数据来源</th><th></th></tr></thead><tbody>';
  cands.forEach((c, ci) => {
    const src = (c.source && c.source.text) || '历史账单';
    h += `<tr><td>${ci + 1}</td><td>${esc(c.model || '')}</td>
      <td>${esc(c.size || '—')}</td><td>${esc(c.material || '—')}</td>
      <td>${c.unit_price != null ? '¥' + c.unit_price : '—'}</td>
      <td>${esc(c.period || '—')}</td>
      <td class="muted">${esc(src)}</td>
      <td><button class="btn primary small" data-adopt="${ci}">采用</button></td></tr>`;
  });
  h += '</tbody></table></div>';
  $('#poPickBody').innerHTML = h;
  $('#poPickBody').querySelectorAll('[data-adopt]').forEach(b => {
    b.onclick = () => poAdoptCandidate(idx, Number(b.dataset.adopt));
  });
  $('#poPickModal').classList.add('show');
}

function poAdoptCandidate(idx, ci) {
  const l = poLines[idx];
  if (!l) return;
  const cands = (l.match && l.match.candidates) || [];
  const c = cands[ci];
  if (!c) return;
  const src = c.source || { text: '历史账单' };
  // 只填这条记录里**确实有值**的字段，空值保持空白，不臆造
  if (c.size) applyField(idx, 'size', c.size, src);
  if (c.material) applyField(idx, 'material', c.material, src);
  if (c.unit_price != null) applyField(idx, 'unit_price', c.unit_price, src);
  if (c.unit_price != null) applyField(idx, 'param', `1 档价 ¥${c.unit_price}`, src);
  l.match = Object.assign({}, l.match, {
    state: 'picked',
    matched: c.model || '',
    msg: `已采用历史记录「${c.model || ''}」（${src.text}）：尺寸 ${c.size || '—'} / 材质 ${c.material || '—'}`
         + (c.unit_price != null ? ` / 单价 ¥${c.unit_price}` : ''),
  });
  $('#poPickModal').classList.remove('show');
  renderEntry();
  toast('已按所选历史记录填充，来源已标注');
}

async function lookupModel(idx) {
  const line = poLines[idx];
  if (!line) return;
  const model = (line.model || '').trim();
  const supplier = $('#poSupplier').value;
  if (!model) { line.match = { state: 'empty', msg: '' }; updateMatchCell(idx); renderMatchSummary(); return; }
  if (!supplier) {
    line.match = { state: 'warn', msg: '请先选择供应商，再按型号匹配历史' };
    updateMatchCell(idx); renderMatchSummary(); return;
  }
  const token = (line._lk = (line._lk || 0) + 1);
  line.match = { state: 'loading', msg: '查询中…' };
  updateMatchCell(idx);
  try {
    const url = '/api/po/model-lookup?supplier=' + encodeURIComponent(supplier)
              + '&model=' + encodeURIComponent(model);
    const r = await fetch(url).then(x => x.json());
    if (line._lk !== token) return;   // 已有更新的查询，丢弃旧结果
    const prevAuto = line._auto || {};
    if (!r.ok) {
      line.match = { state: 'err', msg: r.error || '查询失败' };
    } else if (!r.found) {
      // 未命中：带上跨供应商诊断与相近型号建议，避免只给一句"未找到"
      const extra = (r.suggestions && r.suggestions.length && !r.supplier_hint)
        ? '（历史中相近写法：' + r.suggestions.slice(0, 3).join('、') + '）' : '';
      line.match = { state: 'miss', msg: (r.message || '未找到') + extra, hint: r.supplier_hint || '' };
      // 上一次自动填充的字段已失效，撤掉，避免旧值冒充本次结果
      PO_AUTO_FIELDS.forEach(f => { if (prevAuto[f]) applyField(idx, f, '', null); });
    } else {
      const auto = r.auto || {};
      const amb = r.ambiguous || [];
      const isNear = r.match_type !== 'exact';
      // 只填后端判定「唯一确定」的字段并记录来源；其余留空交给人工选择/填写
      PO_AUTO_FIELDS.forEach(f => {
        if (auto[f]) applyField(idx, f, auto[f].value, auto[f].source);
        else if (prevAuto[f]) applyField(idx, f, '', null);
      });
      line.match = {
        state: isNear ? 'near' : (amb.length ? 'warn' : 'ok'),
        msg: r.message, matched: r.matched_model || '', risk: !!r.risk,
        matchLabel: r.match_label || '', ambiguous: amb,
        candidates: r.candidates || [],
      };
    }
    // 就地更新徽章与「选择」按钮，不整行重绘，避免打断正在输入的其他单元格
    updateMatchCell(idx);
    fillRowInputs(idx);
    renderMatchSummary();
  } catch (e) {
    if (line._lk === token) {
      line.match = { state: 'err', msg: '查询失败：' + e.message };
      updateMatchCell(idx); renderMatchSummary();
    }
  }
}

const PO_FIELDS = ['model', 'param', 'size', 'material', 'quantity', 'unit', 'unit_price', 'required_date', 'remark'];

async function poCreateOrder() {
  const msg = $('#poCreateMsg');
  const errBox = $('#poCreateErrors');
  msg.className = 'msg'; msg.textContent = '';
  errBox.style.display = 'none'; errBox.innerHTML = '';

  const supplier = $('#poSupplier').value;
  const errs = [];
  if (!supplier) errs.push('请选择供应商');
  if (poLines.length === 0) errs.push('请至少添加 1 行产品');
  if (poLines.length > PO_MAX_LINES) errs.push(`产品行数不能超过 ${PO_MAX_LINES} 行`);
  poLines.forEach((l, i) => {
    if (!(l.model || '').trim()) errs.push(`第 ${i + 1} 行：型号不能为空`);
    if (!(l.quantity !== '' && l.quantity !== null && Number(l.quantity) > 0)) errs.push(`第 ${i + 1} 行：数量必须大于 0`);
    if (!l.required_date) errs.push(`第 ${i + 1} 行：交货日期不能为空`);
  });
  if (poLines.length && !poLines.some(l => l.drawingFile)) errs.push('请至少为一行产品上传图纸文件');

  if (errs.length) {
    msg.className = 'msg error';
    msg.textContent = `表单校验未通过（${errs.length} 处），订单未创建`;
    errBox.style.display = '';
    errBox.innerHTML = '<div class="audit-title">请按下列清单修改后重新提交</div>'
      + '<ul class="audit-warns">' + errs.map(x => `<li>${esc(x)}</li>`).join('') + '</ul>';
    return;
  }

  // 存在多条候选但尚未选定的行：提交前二次确认，避免带着不确定信息下单
  const pending = [];
  poLines.forEach((l, i) => {
    const m = l.match || {};
    if (needPick(l) && m.state !== 'picked') pending.push(i + 1);
  });
  if (pending.length) {
    const ok = confirm(`第 ${pending.join('、')} 行在历史账单中有多条候选记录尚未选定，`
      + `尺寸/材质/单价可能不准确。\n\n建议先点该行「选择」按钮确定后再提交。\n\n仍要继续提交吗？`);
    if (!ok) return;
  }

  const items = poLines.map((l, i) => ({
    line_no: i + 1,
    model: (l.model || '').trim(),
    param: l.param || '', size: l.size || '', material: l.material || '',
    quantity: Number(l.quantity), unit: l.unit || '',
    unit_price: (l.unit_price === '' || l.unit_price === null || l.unit_price === undefined)
      ? null : Number(l.unit_price),
    required_date: l.required_date, remark: l.remark || ''
  }));
  const fd = new FormData();
  fd.append('supplier', supplier);
  fd.append('remark', $('#poRemark').value.trim());
  fd.append('items_json', JSON.stringify(items));
  poLines.forEach((l, i) => { if (l.drawingFile) fd.append('drawing_' + (i + 1), l.drawingFile); });

  const btn = $('#btnPoCreate');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const r = await fetch('/api/po/orders', { method: 'POST', body: fd }).then(x => x.json());
    if (!r.ok) {
      msg.className = 'msg error';
      msg.textContent = r.error || '下单失败';
      if (r.errors && r.errors.length) {
        errBox.style.display = '';
        errBox.innerHTML = '<div class="audit-title">服务端校验未通过</div>'
          + '<ul class="audit-warns">' + r.errors.map(x => `<li>${esc(x)}</li>`).join('') + '</ul>';
      }
      return;
    }
    msg.className = 'msg ok'; msg.textContent = r.msg || '下单成功';
    poLines = [blankLine()];
    renderEntry();
    poLoadOrders();
  } catch (e) {
    msg.className = 'msg error'; msg.textContent = '请求失败：' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '提交订单';
  }
}

// ---------- 事件绑定 ----------
$('#btnPoLogin').addEventListener('click', async () => {
  const msg = $('#poLoginMsg');
  msg.className = 'msg'; msg.textContent = '';
  try {
    const r = await fetch('/api/po/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: $('#poUser').value.trim(), password: $('#poPwd').value })
    }).then(x => x.json());
    if (!r.ok) { msg.className = 'msg error'; msg.textContent = r.error || '登录失败'; return; }
    msg.className = 'msg ok'; msg.textContent = '登录成功';
    poState.user = r.user; poShowApp(); poLoadOrders();
    toast('已登录：' + (r.user.display_name || r.user.username));
  } catch (e) { msg.className = 'msg error'; msg.textContent = '请求失败：' + e.message; }
});
$('#poPwd').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btnPoLogin').click(); });

$('#btnPoLogout').addEventListener('click', async () => {
  await fetch('/api/po/logout', { method: 'POST' });
  poState.user = null;
  poLines = [];
  $('#poEntryBody').innerHTML = '';
  poShowLogin();
  toast('已退出采购流程');
});

// 添加行（上限 50）
$('#btnPoAddLine').addEventListener('click', () => {
  if (poLines.length >= PO_MAX_LINES) { toast(`单次最多添加 ${PO_MAX_LINES} 行`); return; }
  poLines.push(blankLine());
  renderEntry();
  const rows = document.querySelectorAll('#poEntryBody tr');
  const last = rows[rows.length - 1];
  const inp = last && last.querySelector('[data-field="model"]');
  if (inp) inp.focus();
});

// 行内输入：写回状态（不重渲染，避免焦点丢失）
$('#poEntryBody').addEventListener('input', e => {
  const el = e.target;
  const field = el.dataset.field;
  if (!field) return;
  const idx = Number(el.dataset.idx);
  if (!poLines[idx] || !PO_FIELDS.includes(field)) return;
  poLines[idx][field] = el.value;
  // 人工改动过的字段不再标「自动填充」，避免来源标注失真
  if (PO_AUTO_FIELDS.includes(field) && poLines[idx]._auto && poLines[idx]._auto[field]) {
    delete poLines[idx]._auto[field];
    el.classList.remove('po-auto');
    el.removeAttribute('title');
  }
  // 型号被改写后，旧的匹配结论与自动填充值都不再成立
  if (field === 'model') {
    const st = poLines[idx].match && poLines[idx].match.state;
    if (st && st !== 'empty' && st !== 'loading') {
      clearAutoFields(idx);
      poLines[idx].match = { state: 'empty', msg: '' };
      updateMatchCell(idx);
      renderMatchSummary();
    }
  }
});
// 型号回车即匹配（不必先点别处）
$('#poEntryBody').addEventListener('keydown', e => {
  const el = e.target;
  if (e.key === 'Enter' && el.dataset && el.dataset.field === 'model') {
    e.preventDefault();
    lookupModel(Number(el.dataset.idx));
  }
});
// 型号失焦：自动匹配历史并回填
$('#poEntryBody').addEventListener('focusout', e => {
  const el = e.target;
  if (el.dataset && el.dataset.field === 'model') {
    const idx = Number(el.dataset.idx);
    if (poLines[idx]) lookupModel(idx);
  }
});
// 行级图纸选择
$('#poEntryBody').addEventListener('change', e => {
  const el = e.target;
  if (!el.dataset.file) return;
  const idx = Number(el.dataset.file);
  const f = el.files && el.files[0];
  if (!f || !poLines[idx]) return;
  const dot = f.name.lastIndexOf('.');
  const ext = dot >= 0 ? f.name.slice(dot).toLowerCase() : '';
  if (!ext || !PO_DOC_EXTS.includes(ext)) {
    toast('图纸格式不支持：' + (ext || '(无扩展名)') + '；支持 ' + PO_DOC_EXTS.length
          + ' 种格式（pdf / dwg / dxf / step / igs / 图片 / Office / zip 等）');
    el.value = '';
    return;
  }
  poLines[idx].drawingFile = f;
  renderEntry();   // 重渲染显示文件名（其他行的输入值从状态恢复）
});
// 删除行 / 移除该行图纸 / 打开候选选择
$('#poEntryBody').addEventListener('click', e => {
  const t = e.target;
  const del = t.dataset && t.dataset.del;
  const clear = t.dataset && t.dataset.clear;
  const pick = t.dataset && t.dataset.pick;
  if (del !== undefined) {
    const idx = Number(del);
    poLines.splice(idx, 1);
    if (poLines.length === 0) poLines.push(blankLine());
    renderEntry();
  } else if (clear !== undefined) {
    const idx = Number(clear);
    if (poLines[idx]) { poLines[idx].drawingFile = null; renderEntry(); }
  } else if (pick !== undefined) {
    e.preventDefault();
    poOpenPicker(Number(pick));
  }
});

// 切换供应商：重置各行匹配状态（匹配口径为「供应商+型号」）
$('#poSupplier').addEventListener('change', () => {
  poLines.forEach(l => {
    if ((l.model || '').trim()) { l.match = { state: 'empty', msg: '' }; }
  });
  renderEntry();
  if (poLines.some(l => (l.model || '').trim())) {
    toast('供应商已变更，请重新触发行匹配（型号失焦即可）');
  }
});

$('#btnPoCreate').addEventListener('click', poCreateOrder);
$('#btnPoClose').addEventListener('click', () => $('#poDetailModal').classList.remove('show'));
$('#poDetailModal').addEventListener('click', e => {
  if (e.target.id === 'poDetailModal') $('#poDetailModal').classList.remove('show');
});
$('#btnPoPickClose').addEventListener('click', () => $('#poPickModal').classList.remove('show'));
$('#poPickModal').addEventListener('click', e => {
  if (e.target.id === 'poPickModal') $('#poPickModal').classList.remove('show');
});

$$('#poFilters .chip').forEach(chip => chip.addEventListener('click', () => {
  $$('#poFilters .chip').forEach(c => c.classList.toggle('active', c === chip));
  poState.filter = chip.dataset.status;
  poLoadOrders();
}));

$$('.tab').forEach(btn => {
  if (btn.dataset.tab === 'po') btn.addEventListener('click', () => poRefreshMe());
});

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', poRefreshMe);
} else {
  poRefreshMe();
}
