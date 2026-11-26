// 账单核对系统前端逻辑
// 本次修复（上线前复盘 A-4/A-5/A-6）：
// - A-4 修复 `const btn=this`（箭头函数 this=window）导致按钮禁用/loading 失效、可重复提交；
// - A-5 成功报告字段与后端对齐，消灭 "undefined"；
// - A-6 文件名渲染统一 esc() 转义（防存储型 XSS）；
// - 补充异步加载容错与「解析失败」筛选。
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
let state = { histFiles: [], rcFiles: [], currentRecon: '', currentStatus: '', currentDiff: '',
              isAdmin: false, suppliers: [], supOpener: '' };

const BTN_HIST_TEXT = '上传并校验（追加）';
const BTN_RC_TEXT = '开始核对';

const STATUS_LABEL = {
  matched_lowest:'已匹配最低价', needs_review:'需人工复核', above_lowest:'高于历史最低价',
  no_history:'无历史记录', parse_error:'解析失败'
};
const STATUS_CLS = {
  matched_lowest:'st-match', needs_review:'st-review', above_lowest:'st-above',
  no_history:'st-review', parse_error:'st-err'
};
// 差异类型标签（由 /api/results 的 summary.by_diff 覆盖补全，这里给静态兜底）
const DIFF_LABEL = {
  amt_high:'金额偏高', amt_low:'金额偏低', no_hist:'无历史基准', parse_err:'单价无法解析',
  multi_level:'多档价待定', date_before:'日期早于基准', base_stale:'基准过期',
  date_missing:'月份缺失', near_model:'型号近似匹配'
};

function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'),2600); }

function fmt(v){ return (v===null||v===undefined||v==='')?'—':(typeof v==='number'?Number(v.toFixed(2)):v); }

// 渲染审查错误清单：文件 / 行号 / 字段 / 值 / 具体问题 / 可能原因与修正建议
function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function renderAuditErrors(errors, warnings){
  if(!errors && !warnings) return '';
  let h='<div class="audit-box">';
  h+='<div class="audit-title">数据审查结果 · 请按下列清单逐条修改后重新上传</div>';
  if(errors && errors.length){
    h+=`<div class="audit-summary bad">✕ 发现 ${errors.length} 处错误，本次全部文件均未录入系统，也未继续处理</div>`;
    h+='<div class="audit-scroll"><table class="audit-table"><thead><tr><th>#</th><th>行号</th><th>字段/列名</th><th>当前值</th><th>具体问题</th><th>可能原因与修正建议</th></tr></thead><tbody>';
    errors.forEach((e,i)=>{
      h+=`<tr><td>${i+1}</td><td class="audit-row">${esc(e.row||'—')}</td><td class="audit-field">${esc(e.field)}</td><td>${esc(e.value)}</td><td class="audit-reason">${esc(e.reason)}</td><td class="audit-cause">${esc(e.cause||'—')}</td></tr>`;
    });
    h+='</tbody></table></div>';
  }
  if(warnings && warnings.length){
    h+=`<div class="audit-summary warn">△ ${warnings.length} 条提示（不影响录入）</div>`;
    h+='<ul class="audit-warns">';
    warnings.forEach(w=>{ h+=`<li>行 ${esc(w.row||'—')} · ${esc(w.field)}：${esc(w.reason)}${w.cause?'　→ '+esc(w.cause):''}</li>`; });
    h+='</ul>';
  }
  h+='</div>';
  return h;
}

// Tabs
$$('.tab').forEach(btn=>btn.addEventListener('click',()=>{
  $$('.tab').forEach(b=>b.classList.toggle('active',b===btn));
  $$('.panel').forEach(p=>p.classList.toggle('active',p.id==='panel-'+btn.dataset.tab));
  if(btn.dataset.tab==='results') loadResultBatches();
  if(btn.dataset.tab==='history') loadHistBatches();
  if(btn.dataset.tab==='reconcile') loadRcBatches();
}));

// Dropzone helper
function bindDrop(dzId, inputId, key){
  const dz=$('#'+dzId), input=$('#'+inputId);
  dz.addEventListener('click',()=>input.click());
  dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('drag');});
  dz.addEventListener('dragleave',()=>dz.classList.remove('drag'));
  dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('drag');
    addFiles(Array.from(e.dataTransfer.files),key);});
  input.addEventListener('change',()=>{addFiles(Array.from(input.files),key);input.value='';});
}
function addFiles(files,key){
  files.forEach(f=>{ if(!state[key].some(x=>x.name===f.name)) state[key].push(f); });
  renderFiles(key);
}
function renderFiles(key){
  const listId = key==='histFiles' ? '#histFileList' : '#rcFileList';
  const el=$(listId); el.innerHTML='';
  state[key].forEach((f,i)=>{
    const d=document.createElement('div'); d.className='fileitem';
    d.innerHTML=`<span>📄</span><span>${esc(f.name)}</span><span class="muted">${(f.size/1024).toFixed(1)} KB</span><span class="x">✕</span>`;
    d.querySelector('.x').onclick=()=>{state[key].splice(i,1);renderFiles(key);};
    el.appendChild(d);
  });
}
bindDrop('histDrop','histFiles','histFiles');
bindDrop('rcDrop','rcFiles','rcFiles');

// Status
async function loadStatus(){
  try{ const r=await fetch('/api/status').then(x=>x.json());
    $('#statusText').textContent=`历史单价 ${r.hist_rows} 条 · 型号基准 ${r.idx_count} 个`;
    $('.dot').classList.toggle('warn',r.hist_rows===0);
  }catch(e){ $('#statusText').textContent='状态加载失败'; }
}

// History upload
$('#btnHistUpload').addEventListener('click',async(ev)=>{
  // A-4 修复：箭头函数内 this=window，改用 ev.currentTarget 获取按钮
  const btn=ev.currentTarget;
  if(state.histFiles.length===0){toast('请先选择文件');return;}
  btn.disabled=true; btn.innerHTML='<span class="loading"></span>上传校验中…';
  $('#histMsg').innerHTML=''; $('#histReport').innerHTML='';
  const fd=new FormData(); state.histFiles.forEach(f=>fd.append('files',f));
  fd.append('supplier',$('#histSupplier').value);
  try{
    const r=await fetch('/api/history/upload',{method:'POST',body:fd}).then(x=>x.json());
    if(!r.ok){
      $('#histMsg').className='msg error'; $('#histMsg').textContent=r.error||'上传失败';
      $('#histReport').innerHTML=renderAuditErrors(r.errors,r.warnings);
      toast('数据审查未通过，未录入任何数据'); return;
    }
    // A-5：字段与后端对齐（supplier/header_row/sheet/total/valid/dup），不再引用不存在的 format/excluded
    let html='';
    r.reports.forEach(rep=>{
      if(rep.ok) html+=`<div class="report-item"><div class="rhead">✓ ${esc(rep.file)} <span class="badge ok">${esc(rep.supplier||'—')}</span></div>
        <div class="muted">表头第 ${rep.header_row} 行（${esc(rep.sheet||'')}）· 总 ${rep.total} 行 · 有效 ${rep.valid} · 重复跳过 ${rep.dup}</div></div>`;
      else html+=`<div class="report-item bad"><div class="rhead">✕ ${esc(rep.file)} <span class="badge bad">失败</span></div><div>${esc(rep.error)}</div></div>`;
    });
    $('#histMsg').className='msg ok';
    $('#histMsg').textContent=(r.msg||`追加成功：历史明细 ${r.hist_rows} 条`) + ` · 型号基准 ${r.idx_count} 个`;
    $('#histReport').innerHTML=html+renderAuditErrors(null,r.warnings);
    state.histFiles=[]; renderFiles('histFiles'); loadStatus(); loadHistBatches(); toast('数据已追加到历史库');
  }catch(e){ $('#histMsg').className='msg error'; $('#histMsg').textContent='请求失败：'+e.message; }
  finally{ btn.disabled=false; btn.textContent=BTN_HIST_TEXT; }
});

$('#btnHistReset').addEventListener('click',async()=>{
  if(!state.isAdmin){ toast('无清空权限'); return; }
  if(!confirm('确认清空全部历史价格库与核对结果？清空前系统会自动备份，但仍请谨慎操作。'))return;
  try{
    const resp=await fetch('/api/history/reset',{method:'POST'});
    const r=await resp.json();
    if(r.ok){
      $('#histMsg').className='msg ok'; $('#histMsg').textContent=r.msg||'已清空历史库';
      toast('历史库已清空（已自动备份）'); loadStatus(); loadHistBatches(); $('#histReport').innerHTML='';
    } else {
      $('#histMsg').className='msg error'; $('#histMsg').textContent=r.error||'清空失败';
      toast('清空失败');
      if(resp.status===403){ state.isAdmin=false; applyAdminUI(); }
    }
  }catch(e){ $('#histMsg').className='msg error'; $('#histMsg').textContent='清空请求失败：'+e.message; }
});

async function loadHistBatches(){
  try{
    const r=await fetch('/api/history/batches').then(x=>x.json());
    const el=$('#histBatches');
    if(!r.batches.length){ el.innerHTML='<div class="empty">暂无历史导入批次</div>'; return; }
    // 有效＝通过校验的行数；入库＝扣除重复后实际写入的条数（import_batch.error_rows 存的是重复跳过数）
    let h='<div class="detail-note">「有效」= 通过校验的行数；「入库」= 扣除重复后实际写入的条数；「重复跳过」按「供应商+型号+单价+账单日期+数量+合计金额+备注」整体判定 —— 日期或明细内容不同即为不同发货，全部入库。</div>';
    h+='<table><thead><tr><th>批次号</th><th>文件</th><th>供应商</th><th>总行</th><th>有效</th><th>入库</th><th>提示</th><th>重复跳过</th><th>状态</th><th>时间</th></tr></thead><tbody>';
    r.batches.forEach(b=>{
      const ins=Math.max(0,(b.valid_rows||0)-(b.error_rows||0));
      h+=`<tr><td>${esc(b.batch_no)}</td><td>${esc(b.file_name)}</td><td>${esc(b.supplier||'—')}</td>
      <td>${b.total_rows}</td><td>${b.valid_rows}</td><td><b>${ins}</b></td><td>${b.excluded_rows}</td>
      <td>${b.error_rows}</td><td>${esc(b.status)}</td><td>${esc(b.created_at)}</td></tr>`; });
    el.innerHTML=h+'</tbody></table>';
  }catch(e){ $('#histBatches').innerHTML='<div class="empty">批次加载失败，请刷新重试</div>'; }
}

// Reconcile
$('#btnReconcile').addEventListener('click',async(ev)=>{
  const btn=ev.currentTarget;   // A-4 修复：同上
  if(state.rcFiles.length===0){toast('请先选择新账单文件');return;}
  btn.disabled=true; btn.innerHTML='<span class="loading"></span>核对中…';
  $('#rcMsg').innerHTML=''; $('#rcReport').innerHTML='';
  const fd=new FormData(); state.rcFiles.forEach(f=>fd.append('files',f));
  fd.append('supplier',$('#rcSupplier').value);
  try{
    const r=await fetch('/api/reconcile',{method:'POST',body:fd}).then(x=>x.json());
    if(!r.ok){
      $('#rcMsg').className='msg error'; $('#rcMsg').textContent=r.error||'核对失败';
      $('#rcReport').innerHTML=renderAuditErrors(r.errors,r.warnings);
      toast('数据审查未通过，未执行核对'); return;
    }
    // A-5：后端 batches 已含 total_rows（此前前端引用 b.total_rows 显示 undefined）
    let html='';
    r.batches.forEach(b=>{
      if(b.ok) html+=`<div class="report-item"><div class="rhead">✓ ${esc(b.file)} <span class="badge ok">核对完成</span></div>
        <div class="muted">批次 ${esc(b.recon_no)} · 总 ${b.total_rows} · <span style="color:var(--green)">已匹配最低价 ${b.matched}</span> ·
        <span style="color:var(--orange)">需复核 ${b.review+b.nohistory+b.error}</span> · 高于最低 ${b.above}</div></div>`;
      else html+=`<div class="report-item bad"><div class="rhead">✕ ${esc(b.file)}</div><div>${esc(b.error)}</div></div>`;
    });
    $('#rcReport').innerHTML=html;
    $('#rcMsg').className='msg ok';
    $('#rcMsg').textContent=`核对完成：共 ${r.total_results} 条记录`;
    state.rcFiles=[]; renderFiles('rcFiles'); loadRcBatches();
    const okb=r.batches.find(b=>b.ok); if(okb){ state.currentRecon=okb.recon_no; }
    toast('核对完成');
  }catch(e){ $('#rcMsg').className='msg error'; $('#rcMsg').textContent='请求失败：'+e.message; }
  finally{ btn.disabled=false; btn.textContent=BTN_RC_TEXT; }
});

async function loadRcBatches(){
  try{
    const r=await fetch('/api/reconcile/batches').then(x=>x.json());
    const el=$('#rcBatches');
    if(!r.batches.length){ el.innerHTML='<div class="empty">暂无核对批次</div>'; return; }
    let h='<table><thead><tr><th>批次号</th><th>文件</th><th>总行</th><th>已匹配最低</th><th>需复核</th><th>高于最低</th><th>无历史</th><th>解析失败</th><th>时间</th></tr></thead><tbody>';
    r.batches.forEach(b=>{ h+=`<tr><td>${esc(b.recon_no)}</td><td>${esc(b.file_name)}</td><td>${b.total_rows}</td>
      <td>${b.n_matched}</td><td>${b.n_review}</td><td>${b.n_above}</td><td>${b.n_nohistory}</td><td>${b.n_error}</td><td>${esc(b.created_at)}</td></tr>`; });
    el.innerHTML=h+'</tbody></table>';
  }catch(e){ $('#rcBatches').innerHTML='<div class="empty">批次加载失败，请刷新重试</div>'; }
}

// Results
async function loadResultBatches(){
  try{
    const r=await fetch('/api/reconcile/batches').then(x=>x.json());
    const sel=$('#resultBatch'); const cur=sel.value;
    sel.innerHTML='<option value="">请选择核对批次</option>';
    r.batches.forEach(b=>{ sel.innerHTML+=`<option value="${esc(b.recon_no)}">${esc(b.recon_no)} · ${esc(b.file_name)} · ${esc(b.created_at)}</option>`; });
    if(cur) sel.value=cur; else if(state.currentRecon) sel.value=state.currentRecon;
    if(sel.value) loadResults();
  }catch(e){ $('#resultTable').innerHTML='<div class="empty">批次加载失败，请刷新重试</div>'; }
}
$('#resultBatch').addEventListener('change',()=>{
  state.currentRecon=$('#resultBatch').value;
  state.currentDiff='';   // 换批次时清掉差异筛选，避免筛出空表
  loadResults();
});
$$('#resultFilters .chip').forEach(chip=>chip.addEventListener('click',()=>{
  $$('#resultFilters .chip').forEach(c=>c.classList.toggle('active',c===chip));
  state.currentStatus=chip.dataset.status; loadResults();
}));

async function loadResults(){
  const no=$('#resultBatch').value;
  if(!no){ $('#resultTable').innerHTML='<div class="empty">请先选择一个核对批次</div>'; return; }
  try{
    const p=new URLSearchParams();
    if(state.currentStatus) p.set('status',state.currentStatus);
    if(state.currentDiff) p.set('diff',state.currentDiff);
    const url='/api/results/'+no+(p.toString()?('?'+p.toString()):'');
    const r=await fetch(url).then(x=>x.json());
    if(!r.ok){ $('#resultTable').innerHTML='<div class="empty">加载失败</div>'; return; }
    const b=r.batch, s=r.summary||{}, amt=s.amount||{};
    (s.by_diff||[]).forEach(d=>{ DIFF_LABEL[d.code]=d.label; });
    // 状态统计（整批口径，不随筛选变化）
    $('#resultStats').innerHTML=`
      <div class="stat"><div class="n">${b.total_rows}</div><div class="l">总记录</div></div>
      <div class="stat green"><div class="n">${b.n_matched}</div><div class="l">已匹配最低价</div></div>
      <div class="stat orange"><div class="n">${b.n_review+b.n_nohistory+b.n_error}</div><div class="l">需人工复核</div></div>
      <div class="stat yellow"><div class="n">${b.n_above}</div><div class="l">高于历史最低价</div></div>`;
    // 金额统计（智能对账）
    $('#resultAmounts').innerHTML=`
      <div class="stat"><div class="n">${money(amt.bill_total)}</div><div class="l">本单金额合计</div>
        <div class="hint">上传账单「合计金额」列合计</div></div>
      <div class="stat"><div class="n">${money(amt.should_total)}</div><div class="l">应有金额合计</div>
        <div class="hint">历史最低价 × 数量（${amt.rows_with_quantity||0} 行可换算）</div></div>
      <div class="stat ${amt.overpay>0?'red':''}"><div class="n">${money(amt.overpay)}</div><div class="l">超付合计</div>
        <div class="hint">高于历史最低价的多付金额</div></div>
      <div class="stat ${amt.diff_rows>0?'orange':'green'}"><div class="n">${amt.diff_rows||0}</div><div class="l">有金额差异的笔数</div>
        <div class="hint">少付合计 ${money(amt.underpay)}</div></div>`;
    renderSupplierSummary(s);
    renderDiffChips(s);

    const el=$('#resultTable');
    if(!r.results.length){ el.innerHTML='<div class="empty">当前筛选无记录</div>'; return; }
    // 明细表头严格按上传账单原表头
    const headers = r.headers && r.headers.length ? r.headers : ['供应商','型号','材质','尺寸','单价','备注'];
    let h='<div class="detail-note">核对明细表头与上传账单完全一致（'+headers.length+' 列）；行底色标识核对状态</div>';
    h+='<table><thead><tr><th>#</th>'+headers.map(x=>`<th>${esc(x)}</th>`).join('')+'<th>核对状态</th></tr></thead><tbody>';
    r.results.forEach((x,i)=>{
      const vals = (x.raw_values||[]).slice(0,headers.length);
      while(vals.length<headers.length) vals.push('');
      const cls = STATUS_CLS[x.status]||'';
      h+=`<tr class="${cls}"><td>${i+1}</td>`+vals.map(v=>`<td>${esc(v==null?'':v)}</td>`).join('')+
         `<td class="${cls}">${STATUS_LABEL[x.status]||esc(x.status)}</td></tr>`;
    });
    h+='</tbody></table>';
    // 差异与依据（派生信息，不并入明细表头）
    h+='<div class="detail-evidence"><div class="audit-title">差异明细与核对依据（派生信息，不在上传账单表头中）</div>'+
       '<table><thead><tr><th>#</th><th>型号</th><th>本单单价</th><th>历史最低价</th><th>单价差异</th><th>差异率</th>'+
       '<th>数量</th><th>金额差异</th><th>基准期间</th><th>差异类型</th><th>状态</th><th>依据与复核原因</th></tr></thead><tbody>';
    r.results.forEach((x,i)=>{
      const tags=(x.diff_list||[]).map(c=>`<span class="difftag">${esc(DIFF_LABEL[c]||c)}</span>`).join('')||'—';
      h+=`<tr><td>${i+1}</td><td>${esc(x.model||'—')}</td><td>${money(x.bill_price)}</td><td>${money(x.lowest_price)}</td>
        <td>${gapCell(x.price_gap)}</td><td>${pct(x.gap_rate)}</td><td>${fmt(x.quantity)}</td><td>${gapCell(x.amount_gap)}</td>
        <td>${esc(x.hist_period||'—')}</td><td>${tags}</td>
        <td class="${STATUS_CLS[x.status]||''}">${STATUS_LABEL[x.status]||esc(x.status)}</td>
        <td class="cell-evidence">${esc(x.evidence||'—')}${x.reason?('<br><span class="cell-reason">'+esc(x.reason)+'</span>'):''}</td></tr>`;
    });
    h+='</tbody></table></div>';
    el.innerHTML=h;
  }catch(e){ $('#resultTable').innerHTML='<div class="empty">结果加载失败，请刷新重试</div>'; }
}

function money(v){ return (v===null||v===undefined||v==='')?'—':Number(v).toFixed(2); }
function pct(v){ return (v===null||v===undefined)?'—':(Number(v)*100).toFixed(2)+'%'; }
function gapCell(v){
  if(v===null||v===undefined||v===''||Math.abs(Number(v))<0.0001) return '—';
  const n=Number(v), cls=n>0?'gap-up':'gap-down';
  return `<span class="${cls}">${n>0?'+':''}${n.toFixed(2)}</span>`;
}

function renderSupplierSummary(s){
  const box=$('#resultSuppliers'), rows=s.by_supplier||[];
  if(!rows.length){ box.style.display='none'; box.innerHTML=''; return; }
  box.style.display='';
  let h='<div class="detail-note">按供应商汇总（智能对账）· 按超付金额降序</div>';
  h+='<table><thead><tr><th>供应商</th><th>记录数</th><th>已匹配最低</th><th>高于最低</th><th>需复核</th>'+
     '<th>无历史</th><th>差异笔数</th><th>超付合计</th><th>少付合计</th><th>最大差异率</th></tr></thead><tbody>';
  rows.forEach(x=>{
    h+=`<tr><td><b>${esc(x.supplier)}</b></td><td>${x.total}</td><td>${x.matched}</td><td>${x.above}</td>
      <td>${x.review}</td><td>${x.nohistory}</td><td>${x.diff_rows}</td>
      <td>${x.overpay?('<span class="gap-up">'+Number(x.overpay).toFixed(2)+'</span>'):'—'}</td>
      <td>${x.underpay?('<span class="gap-down">'+Number(x.underpay).toFixed(2)+'</span>'):'—'}</td>
      <td>${pct(x.max_gap_rate)}</td></tr>`;
  });
  box.innerHTML=h+'</tbody></table>';
}

function renderDiffChips(s){
  const el=$('#diffFilters'), rows=(s.by_diff||[]);
  if(!rows.length){ el.style.display='none'; el.innerHTML=''; return; }
  el.style.display='flex';
  let h=`<button class="chip${state.currentDiff?'':' active'}" data-diff="">全部差异</button>`;
  rows.forEach(d=>{
    h+=`<button class="chip${state.currentDiff===d.code?' active':''}" data-diff="${esc(d.code)}">${esc(d.label)} · ${d.rows}</button>`;
  });
  el.innerHTML=h;
  $$('#diffFilters .chip').forEach(c=>c.addEventListener('click',()=>{
    state.currentDiff=c.dataset.diff||''; loadResults();
  }));
}


$('#btnDownload').addEventListener('click',()=>{
  const no=$('#resultBatch').value; if(!no){toast('请先选择核对批次');return;}
  window.location.href='/api/download/'+no;
});

// ===== 供应商主数据（可扩展：新增后下拉/校验名单自动更新） =====
function supplierTableHTML(){
  if(!state.suppliers.length){
    return '<div class="empty">暂无供应商，请点击「＋ 新增供应商」添加</div>';
  }
  let h='<table><thead><tr><th>供应商名称</th><th>别名</th><th>备注</th><th>历史记录</th><th>核对记录</th><th>创建时间</th><th>操作</th></tr></thead><tbody>';
  state.suppliers.forEach(s=>{
    // 已被历史/核对引用的不允许删除（后端同样会拒绝），仅有管理员身份时显示删除按钮
    const op = (!state.isAdmin) ? '—'
      : (s.in_use ? '<span class="muted">使用中</span>'
                  : `<button class="btn ghost small btn-del" data-del-sup="${s.id}">删除</button>`);
    h+=`<tr><td><b>${esc(s.name)}</b></td><td>${esc(s.alias||'—')}</td><td>${esc(s.remark||'—')}</td>
      <td>${s.hist_rows||0}</td><td>${s.recon_rows||0}</td><td>${esc(s.created_at||'—')}</td><td>${op}</td></tr>`;
  });
  return h+'</tbody></table>';
}

function renderSupplierTable(){
  const html=supplierTableHTML();
  ['#supTable','#supList'].forEach(id=>{ const el=$(id); if(el) el.innerHTML=html; });
  $$('[data-del-sup]').forEach(b=>{ b.onclick=()=>deleteSupplier(b.dataset.delSup); });
}

function renderSupplierSelects(){
  const opts=state.suppliers.map(s=>`<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('');
  $$('.sup-select').forEach(el=>{
    const cur=el.value;
    el.innerHTML='<option value="">自动识别</option>'+opts+'<option value="__add__">＋ 新增供应商…</option>';
    el.value=(cur && cur!=='__add__' && state.suppliers.some(s=>s.name===cur)) ? cur : '';
  });
}

async function loadSuppliers(){
  try{
    const r=await fetch('/api/suppliers').then(x=>x.json());
    if(!r.ok) throw new Error(r.error||'加载失败');
    state.suppliers=r.suppliers||[];
    renderSupplierSelects(); renderSupplierTable(); renderSupplierCheckboxes();
  }catch(e){
    const msg='<div class="empty">供应商加载失败，请刷新重试</div>';
    ['#supTable','#supList'].forEach(id=>{ const el=$(id); if(el) el.innerHTML=msg; });
  }
}

function openSupplierModal(openerSel){
  state.supOpener=openerSel||'';
  $('#supName').value=''; $('#supAlias').value=''; $('#supRemark').value='';
  const m=$('#supMsg'); m.className='msg'; m.textContent='';
  $('#supplierModal').classList.add('show');
  if(!state.suppliers.length) loadSuppliers();
  $('#supName').focus();
}
function closeSupplierModal(){ $('#supplierModal').classList.remove('show'); }

$$('[data-open-sup]').forEach(b=>b.addEventListener('click',()=>{
  const sel=b.closest('.form-row') ? b.closest('.form-row').querySelector('select') : null;
  openSupplierModal(sel ? ('#'+sel.id) : '');
}));
// 下拉里选中「＋ 新增供应商…」即打开弹窗，而不是当成供应商提交
$$('.sup-select').forEach(sel=>sel.addEventListener('change',()=>{
  if(sel.value==='__add__'){ sel.value=''; openSupplierModal('#'+sel.id); }
}));
$('#btnSupCancel').addEventListener('click',closeSupplierModal);
$('#supplierModal').addEventListener('click',e=>{ if(e.target===$('#supplierModal')) closeSupplierModal(); });
document.addEventListener('keydown',e=>{
  if(e.key==='Escape' && $('#supplierModal').classList.contains('show')) closeSupplierModal();
});
$('#supName').addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); submitSupplier(); } });

async function submitSupplier(){
  const name=$('#supName').value.trim();
  const alias=$('#supAlias').value.trim();
  const remark=$('#supRemark').value.trim();
  const msg=$('#supMsg');
  // 前端先挡一道：必填 + 长度（后端仍会完整校验，双保险）
  if(!name){ msg.className='msg error'; msg.textContent='「供应商名称」不能为空'; $('#supName').focus(); return; }
  if(name.length>40){ msg.className='msg error'; msg.textContent='「供应商名称」最长 40 个字符'; return; }
  const btn=$('#btnSupAdd'); btn.disabled=true; btn.textContent='保存中…';
  try{
    const resp=await fetch('/api/suppliers',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name,alias:alias,remark:remark})});
    const r=await resp.json();
    if(!r.ok){ msg.className='msg error'; msg.textContent=r.error||'保存失败'; return; }
    state.suppliers=r.suppliers||[];
    renderSupplierSelects(); renderSupplierTable(); renderSupplierCheckboxes();
    msg.className='msg ok'; msg.textContent=r.msg||('已新增供应商「'+r.supplier.name+'」');
    $('#supName').value=''; $('#supAlias').value=''; $('#supRemark').value='';
    // 从下拉点进来的：直接把新供应商选上，省一次选择
    if(state.supOpener){
      const el=$(state.supOpener);
      if(el && Array.from(el.options).some(o=>o.value===r.supplier.name)) el.value=r.supplier.name;
    }
    toast('供应商「'+r.supplier.name+'」已添加');
    $('#supName').focus();
  }catch(e){ msg.className='msg error'; msg.textContent='请求失败：'+e.message; }
  finally{ btn.disabled=false; btn.textContent='保存供应商'; }
}
$('#btnSupAdd').addEventListener('click',submitSupplier);

async function deleteSupplier(sid){
  if(!state.isAdmin){ toast('需要管理员权限才能删除'); return; }
  const row=state.suppliers.find(s=>String(s.id)===String(sid));
  const name=row?row.name:('#'+sid);
  if(!confirm('确认删除供应商「'+name+'」？该操作不可恢复。')) return;
  try{
    const resp=await fetch('/api/suppliers/'+sid+'/delete',{method:'POST'});
    const r=await resp.json();
    if(!r.ok){ toast(r.error||'删除失败'); if(resp.status===403){ state.isAdmin=false; applyAdminUI(); } return; }
    state.suppliers=r.suppliers||[];
    renderSupplierSelects(); renderSupplierTable(); renderSupplierCheckboxes();
    const msg=$('#supMsg'); msg.className='msg ok'; msg.textContent=r.msg||'已删除';
    toast('供应商「'+name+'」已删除');
  }catch(e){ toast('删除请求失败：'+e.message); }
}

// ===== 账单备份导出（筛选 → 预览 → 打包下载） =====
function renderSupplierCheckboxes(){
  const box=$('#bkSuppliers'); if(!box) return;
  const hasInputs = box.querySelectorAll('input').length > 0;
  const prev = hasInputs ? new Set($$('#bkSuppliers input:checked').map(x=>x.value)) : null;
  if(!state.suppliers.length){ box.innerHTML='<span class="muted">暂无供应商档案</span>'; return; }
  box.innerHTML = state.suppliers.map(s=>{
    const on = prev ? prev.has(s.name) : true;   // 默认全选＝导出全部供应商
    return `<label class="chk"><input type="checkbox" value="${esc(s.name)}"${on?' checked':''}> ${esc(s.name)}</label>`;
  }).join('');
}

function bkFilters(){
  const kinds=[];
  if($('#bkKindHistory').checked) kinds.push('history');
  if($('#bkKindRecon').checked) kinds.push('recon');
  return {
    kinds: kinds.join(','),
    suppliers: $$('#bkSuppliers input:checked').map(x=>x.value).join(','),
    month_from: $('#bkFrom').value,
    month_to: $('#bkTo').value,
    include_excluded: $('#bkInclExcluded').checked ? '1' : ''
  };
}
function bkQuery(){
  const o=bkFilters();
  return Object.keys(o).filter(k=>o[k]).map(k=>k+'='+encodeURIComponent(o[k])).join('&');
}

$('#btnBkPreview').addEventListener('click',async()=>{
  const f=bkFilters(), msg=$('#bkMsg'), box=$('#bkPreview');
  if(!f.kinds){ toast('请至少选择一种账单类型'); return; }
  msg.className='msg'; msg.textContent='正在统计…'; box.innerHTML='';
  try{
    const r=await fetch('/api/history/backup/preview?'+bkQuery()).then(x=>x.json());
    if(!r.ok){ msg.className='msg error'; msg.textContent=r.error||'预览失败'; return; }
    if(!r.totals.rows){
      msg.className='msg error';
      msg.textContent='当前筛选条件下没有可导出的账单数据，请放宽时间范围/供应商/账单类型';
      return;
    }
    msg.className='msg ok';
    msg.textContent=`将导出 ${r.totals.files} 个文件 / ${r.totals.rows} 条记录（历史 ${r.totals.history_rows} · 核对 ${r.totals.recon_rows}），金额合计 ${Number(r.totals.amount).toFixed(2)} 元；ZIP 名称：${r.zip_name}`;
    let h='<div class="audit-box"><div class="audit-title">将打包的文件清单（分表为标准录入模板 9 列，可直接回灌）</div><div class="audit-scroll">'+
          '<table class="audit-table"><thead><tr><th>文件名</th><th>类型</th><th>供应商</th><th>期间</th><th>行数</th><th>日期范围</th><th>金额合计</th><th>日期补齐</th></tr></thead><tbody>';
    r.files.forEach(x=>{
      h+=`<tr><td>${esc(x.name)}</td><td>${esc(x.kind_label)}</td><td>${esc(x.supplier)}</td><td>${esc(x.month)}</td>
        <td>${x.rows}</td><td>${esc(x.date_from||'—')} ~ ${esc(x.date_to||'—')}</td>
        <td>${Number(x.amount).toFixed(2)}</td><td>${x.date_filled||0}</td></tr>`;
    });
    box.innerHTML=h+'</tbody></table></div></div>';
  }catch(e){ msg.className='msg error'; msg.textContent='预览失败：'+e.message; }
});

$('#btnBkDownload').addEventListener('click',()=>{
  const f=bkFilters(), msg=$('#bkMsg');
  if(!f.kinds){ toast('请至少选择一种账单类型'); return; }
  msg.className='msg ok';
  msg.textContent='正在打包，浏览器会自动下载 ZIP…';
  window.location.href='/api/history/backup?'+bkQuery();
});

// ===== 管理员权限（清空历史库） =====
function applyAdminUI(){
  const admin=state.isAdmin;
  $('#btnHistReset').style.display=admin?'':'none';
  $('#btnAdminLogin').style.display=admin?'none':'';
  $('#btnAdminLogout').style.display=admin?'':'none';
  $('#adminState').textContent=admin?'当前身份：管理员（可清空历史库 / 删除供应商）':'当前身份：普通用户（无清空权限）';
  $('#adminState').classList.toggle('is-admin',admin);
  // 删除按钮的可见性取决于管理员身份，身份变化后重新渲染供应商表格
  if(state.suppliers.length) renderSupplierTable();
}
async function loadAuth(){
  try{
    const r=await fetch('/api/auth/status').then(x=>x.json());
    state.isAdmin=!!r.is_admin;
    applyAdminUI();
  }catch(e){}
}
$('#btnAdminLogin').addEventListener('click',()=>{
  $('#adminPassword').value=''; $('#adminMsg').textContent='';
  $('#adminModal').classList.add('show'); $('#adminPassword').focus();
});
$('#btnAdminCancel').addEventListener('click',()=>$('#adminModal').classList.remove('show'));
$('#btnAdminConfirm').addEventListener('click',async()=>{
  try{
    const resp=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({password:$('#adminPassword').value})});
    const r=await resp.json();
    if(r.ok){
      state.isAdmin=true; applyAdminUI();
      $('#adminModal').classList.remove('show');
      $('#histMsg').className='msg ok'; $('#histMsg').textContent=r.msg||'已获得清空权限';
      toast('管理员登录成功');
    } else {
      $('#adminMsg').className='msg error'; $('#adminMsg').textContent=r.error||'登录失败';
    }
  }catch(e){ $('#adminMsg').className='msg error'; $('#adminMsg').textContent='请求失败：'+e.message; }
});
$('#btnAdminLogout').addEventListener('click',async()=>{
  await fetch('/api/auth/logout',{method:'POST'});
  state.isAdmin=false; applyAdminUI();
  $('#histMsg').className='msg ok'; $('#histMsg').textContent='已退出管理员模式';
  toast('已退出管理员');
});

loadStatus(); loadHistBatches(); loadAuth(); loadSuppliers();
