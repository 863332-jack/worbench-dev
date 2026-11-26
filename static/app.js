// 账单核对系统前端逻辑
// 本次修复（上线前复盘 A-4/A-5/A-6）：
// - A-4 修复 `const btn=this`（箭头函数 this=window）导致按钮禁用/loading 失效、可重复提交；
// - A-5 成功报告字段与后端对齐，消灭 "undefined"；
// - A-6 文件名渲染统一 esc() 转义（防存储型 XSS）；
// - 补充异步加载容错与「解析失败」筛选。
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
let state = { histFiles: [], rcFiles: [], currentRecon: '', currentStatus: '', isAdmin: false };

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
    let h='<table><thead><tr><th>批次号</th><th>文件</th><th>供应商</th><th>总行</th><th>有效</th><th>提示</th><th>重复</th><th>状态</th><th>时间</th></tr></thead><tbody>';
    r.batches.forEach(b=>{ h+=`<tr><td>${esc(b.batch_no)}</td><td>${esc(b.file_name)}</td><td>${esc(b.supplier||'—')}</td>
      <td>${b.total_rows}</td><td>${b.valid_rows}</td><td>${b.excluded_rows}</td><td>${b.error_rows}</td><td>${esc(b.status)}</td><td>${esc(b.created_at)}</td></tr>`; });
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
$('#resultBatch').addEventListener('change',()=>{ state.currentRecon=$('#resultBatch').value; loadResults(); });
$$('#resultFilters .chip').forEach(chip=>chip.addEventListener('click',()=>{
  $$('#resultFilters .chip').forEach(c=>c.classList.toggle('active',c===chip));
  state.currentStatus=chip.dataset.status; loadResults();
}));

async function loadResults(){
  const no=$('#resultBatch').value; if(!no){ $('#resultTable').innerHTML='<div class="empty">请先选择一个核对批次</div>'; return; }
  try{
    const url='/api/results/'+no+(state.currentStatus?('?status='+state.currentStatus):'');
    const r=await fetch(url).then(x=>x.json());
    if(!r.ok){ $('#resultTable').innerHTML='<div class="empty">加载失败</div>'; return; }
    const b=r.batch;
    $('#resultStats').innerHTML=`
      <div class="stat"><div class="n">${b.total_rows}</div><div class="l">总记录</div></div>
      <div class="stat green"><div class="n">${b.n_matched}</div><div class="l">已匹配最低价</div></div>
      <div class="stat orange"><div class="n">${b.n_review+b.n_nohistory+b.n_error}</div><div class="l">需人工复核</div></div>
      <div class="stat yellow"><div class="n">${b.n_above}</div><div class="l">高于历史最低价</div></div>`;
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
    // 依据与复核原因单独呈现，不并入明细表头
    h+='<div class="detail-evidence"><div class="audit-title">核对依据与复核原因（派生信息，不在上传账单表头中）</div><table><thead><tr><th>#</th><th>型号</th><th>状态</th><th>依据</th><th>复核原因</th></tr></thead><tbody>';
    r.results.forEach((x,i)=>{
      h+=`<tr><td>${i+1}</td><td>${esc(x.model||'—')}</td><td class="${STATUS_CLS[x.status]||''}">${STATUS_LABEL[x.status]||esc(x.status)}</td><td class="cell-evidence">${esc(x.evidence||'—')}</td><td class="cell-reason">${esc(x.reason||'—')}</td></tr>`;
    });
    h+='</tbody></table></div>';
    el.innerHTML=h;
  }catch(e){ $('#resultTable').innerHTML='<div class="empty">结果加载失败，请刷新重试</div>'; }
}

$('#btnDownload').addEventListener('click',()=>{
  const no=$('#resultBatch').value; if(!no){toast('请先选择核对批次');return;}
  window.location.href='/api/download/'+no;
});

// ===== 管理员权限（清空历史库） =====
function applyAdminUI(){
  const admin=state.isAdmin;
  $('#btnHistReset').style.display=admin?'':'none';
  $('#btnAdminLogin').style.display=admin?'none':'';
  $('#btnAdminLogout').style.display=admin?'':'none';
  $('#adminState').textContent=admin?'当前身份：管理员（可清空历史库）':'当前身份：普通用户（无清空权限）';
  $('#adminState').classList.toggle('is-admin',admin);
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

loadStatus(); loadHistBatches(); loadAuth();
