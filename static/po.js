// 采购工程流程前端（角色：采购方 / 供应商）
// 下单：多行产品表格（1-50 行），型号自动匹配历史账单回填参数/尺寸/材料/单价
const PO_LABELS = { pending_confirm: '待确认', confirmed: '已接单', received: '已收货', rejected: '已拒接' };
const PO_CLS = { pending_confirm: 'po-st-pending', confirmed: 'po-st-confirmed',
                 received: 'po-st-received', rejected: 'po-st-rejected' };
const PO_MAX_LINES = 50;
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
           match: { state: 'empty', msg: '' } };
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
          <input id="poRcvFile" type="file" accept=".pdf,.png,.jpg,.jpeg,.xlsx,.xls"></div>
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
  const map = { ok: '✓ 已匹配', warn: '⚠ 需确认', miss: '✕ 未找到', err: '✕ 查询失败', loading: '查询中…', empty: '—' };
  return map[m && m.state] || '—';
}
function matchClass(m) {
  return { ok: 'po-ok', warn: 'po-warn', miss: 'po-miss', err: 'po-miss', loading: 'po-loading' }[(m && m.state)] || '';
}

function rowHtml(l, i) {
  const fileCell = l.drawingFile
    ? `<span class="po-file-name" title="${esc(l.drawingFile.name)}">📎 ${esc(l.drawingFile.name)}</span>
       <span class="po-file-x" data-clear="${i}" title="移除该行图纸">✕</span>`
    : `<input type="file" class="po-file-input" data-file="${i}" accept=".pdf,.dwg,.dxf,.png,.jpg,.jpeg,.xlsx,.xls">`;
  const price = (l.unit_price === null || l.unit_price === undefined) ? '' : l.unit_price;
  return `<tr data-idx="${i}">
    <td class="c-line">${i + 1}</td>
    <td><input class="po-cell-input po-cell-model" data-idx="${i}" data-field="model" value="${esc(l.model || '')}" placeholder="型号"></td>
    <td class="c-match"><span class="po-match ${matchClass(l.match)}" title="${esc((l.match && l.match.msg) || '')}">${matchShort(l.match)}</span></td>
    <td><input class="po-cell-input" data-idx="${i}" data-field="param" value="${esc(l.param || '')}" placeholder="自动"></td>
    <td><input class="po-cell-input" data-idx="${i}" data-field="size" value="${esc(l.size || '')}" placeholder="自动"></td>
    <td><input class="po-cell-input" data-idx="${i}" data-field="material" value="${esc(l.material || '')}" placeholder="自动"></td>
    <td><input class="po-cell-input num" type="number" min="0.001" step="any" data-idx="${i}" data-field="quantity" value="${esc(l.quantity || '')}" placeholder="数量"></td>
    <td><input class="po-cell-input narrow" data-idx="${i}" data-field="unit" value="${esc(l.unit || '')}" placeholder="件"></td>
    <td><input class="po-cell-input num" type="number" min="0" step="any" data-idx="${i}" data-field="unit_price" value="${esc(price)}" placeholder="参考"></td>
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
  const span = tr.querySelector('.po-match');
  if (!span) return;
  const m = poLines[idx].match || { state: 'empty', msg: '' };
  span.className = 'po-match ' + matchClass(m);
  span.textContent = matchShort(m);
  span.title = m.msg || '';
}

function fillRowInputs(idx) {
  const tr = document.querySelector(`#poEntryBody tr[data-idx="${idx}"]`);
  if (!tr) return;
  const l = poLines[idx];
  ['param', 'size', 'material', 'unit_price'].forEach(f => {
    const el = tr.querySelector(`[data-field="${f}"]`);
    if (el) el.value = (l[f] === null || l[f] === undefined) ? '' : l[f];
  });
}

function renderMatchSummary() {
  const miss = [], warn = [];
  poLines.forEach((l, i) => {
    if (!(l.model || '').trim()) return;
    if (l.match && l.match.state === 'miss') miss.push(i + 1);
    if (l.match && l.match.state === 'warn') warn.push(i + 1);
  });
  const el = $('#poMatchSummary');
  if (!el) return;
  let h = '';
  if (miss.length) {
    h += `<div class="po-note warn">✕ 第 ${miss.join('、')} 行型号未找到历史记录：可手工填写参数/尺寸/材料后继续下单，或核对型号是否正确。</div>`;
  }
  if (warn.length) {
    h += `<div class="po-note warn">⚠ 第 ${warn.join('、')} 行匹配到多条历史记录（多档价）：已回填最低价，请人工确认适用单价。</div>`;
  }
  el.innerHTML = h;
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
    if (!r.ok) {
      line.match = { state: 'err', msg: r.error || '查询失败' };
    } else if (!r.found) {
      line.match = { state: 'miss', msg: r.message };
    } else {
      line.match = { state: r.risk ? 'warn' : 'ok', msg: r.message };
      if (r.param) line.param = r.param;
      if (r.size) line.size = r.size;
      if (r.material) line.material = r.material;
      if (r.unit_price !== null && r.unit_price !== undefined) line.unit_price = r.unit_price;
    }
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
  const ext = f.name.toLowerCase().slice(f.name.lastIndexOf('.'));
  const allow = ['.pdf', '.dwg', '.dxf', '.png', '.jpg', '.jpeg', '.xlsx', '.xls'];
  if (!allow.includes(ext)) {
    toast('图纸格式不支持：' + ext + '（允许 ' + allow.join(' ') + '）');
    el.value = '';
    return;
  }
  poLines[idx].drawingFile = f;
  renderEntry();   // 重渲染显示文件名（其他行的输入值从状态恢复）
});
// 删除行 / 移除该行图纸
$('#poEntryBody').addEventListener('click', e => {
  const del = e.target.dataset && e.target.dataset.del;
  const clear = e.target.dataset && e.target.dataset.clear;
  if (del !== undefined) {
    const idx = Number(del);
    poLines.splice(idx, 1);
    if (poLines.length === 0) poLines.push(blankLine());
    renderEntry();
  } else if (clear !== undefined) {
    const idx = Number(clear);
    if (poLines[idx]) { poLines[idx].drawingFile = null; renderEntry(); }
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
