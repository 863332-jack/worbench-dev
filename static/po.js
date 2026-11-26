// 采购工程流程前端（角色：采购方 / 供应商）
// 流程：采购方上传图纸下单 → 待确认 → 供应商确认接单 → 已接单 → 采购方登记收货 → 已收货
const PO_LABELS = { pending_confirm: '待确认', confirmed: '已接单', received: '已收货', rejected: '已拒接' };
const PO_CLS = { pending_confirm: 'po-st-pending', confirmed: 'po-st-confirmed',
                 received: 'po-st-received', rejected: 'po-st-rejected' };
let poState = { user: null, filter: '', current: null };

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
    ? '点击订单行查看详情；「已接单」的订单可登记收货'
    : '点击订单行查看详情、下载图纸并确认接单';
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

async function poLoadOrders() {
  if (!poState.user) return;
  const q = poState.filter ? '?status=' + poState.filter : '';
  try {
    const r = await fetch('/api/po/orders' + q).then(x => x.json());
    if (!r.ok) { $('#poOrders').innerHTML = '<div class="empty">加载失败</div>'; return; }
    poRenderStats(r.stats);
    const el = $('#poOrders');
    if (!r.orders.length) { el.innerHTML = '<div class="empty">暂无订单</div>'; return; }
    let h = '<table><thead><tr><th>订单号</th><th>供应商</th><th>物料 / 型号</th><th>数量</th>'
          + '<th>期望交期</th><th>状态</th><th>下单时间</th><th></th></tr></thead><tbody>';
    r.orders.forEach(o => {
      h += `<tr><td>${esc(o.order_no)}</td><td>${esc(o.supplier)}</td>
        <td>${esc(o.product_name)}${o.model ? ' / ' + esc(o.model) : ''}</td>
        <td>${esc(o.quantity)}${esc(o.unit || '')}</td>
        <td>${esc(o.required_date || '—')}</td>
        <td>${poBadge(o.status)}</td>
        <td class="muted">${esc(o.created_at || '')}</td>
        <td><button class="btn ghost small" data-detail="${esc(o.order_no)}">详情</button></td></tr>`;
    });
    el.innerHTML = h + '</tbody></table>';
    el.querySelectorAll('[data-detail]').forEach(b => b.onclick = () => poOpenDetail(b.dataset.detail));
  } catch (e) { $('#poOrders').innerHTML = '<div class="empty">加载失败，请刷新</div>'; }
}

async function poOpenDetail(no) {
  try {
    const r = await fetch('/api/po/orders/' + no).then(x => x.json());
    if (!r.ok) { toast(r.error || '加载失败'); return; }
    poState.current = r;
    const o = r.order;
    $('#poDetailTitle').textContent = '订单详情 · ' + o.order_no;
    let h = '<div class="po-kv">'
      + kv('订单号', esc(o.order_no)) + kv('状态', poBadge(o.status))
      + kv('供应商', esc(o.supplier)) + kv('采购员', esc(o.purchaser))
      + kv('物料名称', esc(o.product_name)) + kv('型号', esc(o.model || ''))
      + kv('数量', esc(o.quantity) + esc(o.unit || ''))
      + kv('单价', o.unit_price != null ? '¥' + o.unit_price : '')
      + kv('金额', o.total_amount != null ? '¥' + o.total_amount : '')
      + kv('期望交期', esc(o.required_date || ''))
      + kv('下单时间', esc(o.created_at || ''))
      + kv('确认时间', esc(o.confirmed_at || ''))
      + kv('确认人', esc(o.confirmed_by || ''))
      + (o.remark ? kv('备注', esc(o.remark)) : '')
      + '</div>';

    h += '<div class="po-files">附件：';
    if (o.drawing_name) h += `<a class="po-file" href="/api/po/orders/${esc(o.order_no)}/file?type=drawing">📎 图纸 · ${esc(o.drawing_name)}</a>`;
    else h += '<span class="muted">无图纸</span>';
    if (o.receipt_name) h += `<a class="po-file" href="/api/po/orders/${esc(o.order_no)}/file?type=receipt">📎 收货凭证 · ${esc(o.receipt_name)}</a>`;
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

async function poCreateOrder() {
  const msg = $('#poCreateMsg');
  msg.className = 'msg'; msg.textContent = '';
  const payload = {
    supplier: $('#poSupplier').value,
    product_name: $('#poProduct').value.trim(),
    model: $('#poModel').value.trim(),
    quantity: $('#poQty').value.trim(),
    unit: $('#poUnit').value.trim(),
    unit_price: $('#poPrice').value.trim(),
    required_date: $('#poDue').value,
    remark: $('#poRemark').value.trim(),
  };
  const drawing = $('#poDrawing').files[0];
  // 前端基础校验（后端同规则兜底）
  const errs = [];
  if (!payload.supplier) errs.push('请选择供应商');
  if (!payload.product_name) errs.push('物料名称不能为空');
  if (!payload.quantity || Number(payload.quantity) <= 0) errs.push('数量必须大于 0');
  if (payload.unit_price && Number(payload.unit_price) < 0) errs.push('单价不能为负数');
  if (!drawing) errs.push('请上传图纸文件');
  if (errs.length) { msg.className = 'msg error'; msg.textContent = errs.join('；'); return; }

  const fd = new FormData();
  Object.keys(payload).forEach(k => fd.append(k, payload[k]));
  fd.append('drawing', drawing);
  const btn = $('#btnPoCreate');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const r = await fetch('/api/po/orders', { method: 'POST', body: fd }).then(x => x.json());
    if (!r.ok) {
      msg.className = 'msg error';
      msg.textContent = (r.error || '下单失败') + (r.errors ? '：' + r.errors.join('；') : '');
      return;
    }
    msg.className = 'msg ok'; msg.textContent = r.msg || '下单成功';
    $('#poProduct').value = ''; $('#poModel').value = ''; $('#poQty').value = '';
    $('#poUnit').value = ''; $('#poPrice').value = ''; $('#poDue').value = '';
    $('#poRemark').value = ''; $('#poDrawing').value = '';
    poLoadOrders();
  } catch (e) { msg.className = 'msg error'; msg.textContent = '请求失败：' + e.message; }
  finally { btn.disabled = false; btn.textContent = '提交订单'; }
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
  poShowLogin();
  toast('已退出采购流程');
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

// 切换到采购 Tab 时刷新身份与列表
$$('.tab').forEach(btn => {
  if (btn.dataset.tab === 'po') {
    btn.addEventListener('click', () => poRefreshMe());
  }
});

// 初始化（未登录则显示登录卡片，面板本身默认隐藏）
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', poRefreshMe);
} else {
  poRefreshMe();
}
