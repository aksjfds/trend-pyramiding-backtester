'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="panel-token"]').content;
const labels = {watch:'只读观察', 'demo-watch':'模拟账户观察', demo:'模拟交易', live:'实盘交易'};
let marketCatalog=[], catalogProfile=null, catalogLoading=false, selectedManualInstrument=null;
let settingsEditor=null, settingsSaving=false, settingsProfile=null;
const accountAttempts = {};
let last = null, actionBusy = false, checkBusy = false, loading = false, connected = false, networkLoading = false;
const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', {hour12:false}) : '—';
const num = value => value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:6});
const usdt = value => value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}) + ' USDT';
const pct = value => value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2}) + '%';
async function api(path, data) {
  const controller = new AbortController();
  const timeout = setTimeout(()=>controller.abort(), path === '/api/check' ? 130000 : path.includes('/api/instruments') ? 60000 : 15000);
  try {
  const response = await fetch(path, {signal:controller.signal, cache:'no-store', headers:{'X-Panel-Token':token, ...(data ? {'Content-Type':'application/json'} : {})}, ...(data ? {method:'POST', body:JSON.stringify(data)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，请查看当前状态后重试');
    throw error;
  } finally {clearTimeout(timeout);}
}
function message(text) { text = text.replace('missing OKX environment variables: ', '尚未配置完整的环境变量：'); $('error').textContent = text; $('error').hidden = !text; }
function controls() {
  const mode = $('mode').value;
  const active = last?.running;
  $('mode').disabled = !!active || actionBusy || checkBusy;
  $('start').disabled = !connected || !!active || actionBusy || checkBusy || !!last?.external_process?.length || !!settingsEditor || settingsSaving;
  $('stop').disabled = !connected || !active || !!last?.stopping || actionBusy;
  $('check').disabled = !connected || checkBusy || !!last?.checking;
  $('check').textContent = checkBusy || last?.checking ? '正在读取…' : '刷新账户 ↗';
  $('generate-candidates').disabled =
    !connected ||
    !last?.candidate_scan_enabled ||
    !!last?.candidate_scan_pending ||
    actionBusy;
  $('generate-candidates').textContent =
    last?.candidate_scan_pending ? '生成中…' : '生成候选';
  $('manual-entry-instrument').disabled =
    !connected || !last?.manual_entry_enabled || !!last?.manual_entry_pending || actionBusy;
  $('manual-entry-fraction').disabled =
    !connected || !last?.manual_entry_enabled || !!last?.manual_entry_pending || actionBusy;
  const manualFraction = Number($('manual-entry-fraction').value);
  const manualFractionValid = Number.isFinite(manualFraction) && manualFraction > 0 && manualFraction <= 100;
  $('manual-entry-submit').disabled =
    !connected ||
    !last?.manual_entry_enabled ||
    !!last?.manual_entry_pending ||
    actionBusy ||
    !manualInstrumentMatch($('manual-entry-instrument').value) ||
    !manualFractionValid;
  $('manual-entry-submit').textContent =
    last?.manual_entry_pending ? '提交中…' : '开仓并交给策略接管';
  $('start').textContent = actionBusy ? '处理中…' : mode === 'live' ? '▶ 启动实盘交易' : mode === 'demo' ? '▶ 启动模拟交易' : '▶ 启动观察';
  $('start').classList.toggle('live', mode === 'live');
  const settingsLocked = !!last?.settings_locked || !!last?.external_process?.length || checkBusy || !!last?.checking || settingsSaving;
  for (const [id, kind] of [['edit-capital','capital'],['edit-strategy','strategy']]) {
    const button=$(id);
    button.disabled = !connected || settingsLocked || (!!settingsEditor && settingsEditor !== kind);
    button.textContent = settingsSaving && settingsEditor === kind ? '保存中…' : settingsEditor === kind ? '保存' : '修改';
  }
  $('stop').textContent = last?.stopping ? '正在停止…' : '■ 停止';
  $('mode-note').textContent = mode === 'live' ? '启动后会使用实盘账户下单，资金上限按策略配置执行。' : mode === 'demo' ? '使用独立模拟盘密钥，向 OKX 模拟账户发送订单。' : '持续检查账户与市场，不发送交易订单。';
}
function render(s) {
  last = s;
  if (s.running && s.mode) $('mode').value = s.mode;
  const warnings = [];
  if (s.halt) warnings.push('策略异常暂停：' + s.halt.detail + '。核查订单后在终端执行 clear-halt，再重新启动。');
  if (s.pending) warnings.push('存在尚待核对的订单，请保留状态记录。');
  if (s.state_error) warnings.push(s.state_error);
  if (s.external_process.length) warnings.push('检测到其他终端启动的策略。本页仅能停止从当前网页启动的进程。');
  const beat = s.heartbeat;
  if (s.running && beat && Date.now()/1000 - beat.updated_at > 600) warnings.push('策略超过 10 分钟没有更新，请检查最近日志。');
  if (s.running && beat?.phase === 'recovering') warnings.push('网络或交易所暂不可用，正在自动重连。');
  if (s.running && beat?.phase === 'degraded') warnings.push('部分币种行情暂不可用，已限制相关交易，详情见日志。');
  if (s.account?.error) warnings.push('最近账户读取失败：' + s.account.error);
  warnings.push(...(s.account?.data?.warnings || []));
  $('warning').textContent = warnings.join(' '); $('warning').hidden = !warnings.length;
  const phase = s.stopping ? '正在停止' : s.running ? (s.halt ? '异常暂停' : beat?.phase === 'recovering' ? '连接恢复中' : beat?.phase === 'degraded' ? '运行中 · 部分行情暂不可用' : beat?.phase === 'error' ? '检查失败' : beat?.phase === 'ready' ? labels[s.mode] + '运行中' : '正在启动') : s.external_process.length ? '外部进程运行中' : s.exit_code !== null && s.exit_code !== 0 ? '进程已退出' : '未运行';
  $('run-badge').textContent = phase;
  $('run-badge').className = 'badge ' + (s.halt || (s.exit_code && !s.running) ? 'danger' : s.running ? '' : 'neutral');
  $('profile').textContent = s.profile === 'demo' ? '模拟账户' : '实盘账户';
  $('keys').textContent = s.credentials[s.profile] ? '已配置' : '未配置完整';
  $('key-note').textContent = s.credentials[s.profile] ? '密钥已由服务读取，无需重复输入。' : '请配置此账户的密钥后，重新启动网页服务。';
  $('started').textContent = s.started_at ? date(s.started_at) : '尚未启动';
  $('heartbeat').textContent = beat ? date(beat.updated_at) : '—';
  if (settingsEditor !== 'strategy') {
    $('bar').textContent = s.config.bar;
    $('leverage').textContent = s.config.leverage + '×';
  }
  if (settingsEditor !== 'capital') {
    $('allocation').replaceChildren(
      document.createTextNode(Math.round(s.config.capital_fraction*10000)/100),
      Object.assign(document.createElement('span'),{textContent:'%'})
    );
  }
  const account = s.account?.data;
  $('equity').textContent = num(account?.account_equity_usdt_equivalent);
  $('available').textContent = num(account?.available_usdt);
  $('account-time').textContent = s.account?.time ? '更新于 ' + date(s.account.time) : s.credentials[s.profile] ? '正在读取账户…' : '尚未配置此账户';
  $('account-orders').textContent = account ? `${account.open_positions ?? "—"} / ${account.pending_orders ?? "—"}` : '—';
  $('budget').textContent = account ? '当前权益对应上限 ' + num(account.capital_limit_usdt_approx) + ' USDT' : '保证金与手续费预留';
  const positions = account?.positions || [];
  $('position-count').replaceChildren(document.createTextNode(positions.length), Object.assign(document.createElement('span'), {textContent:' 个仓位'}));
  $('pending').textContent = s.pending ? '有待核对订单' : 'OKX 实际仓位 · 无待核对订单';
  $('market-count').textContent = positions.length ? `${positions.length} 个仓位` : '当前无仓位';
  $('position-list').replaceChildren(...positions.map(row=>{
    const card=document.createElement('article');
    card.className='position-item';
    const title=document.createElement('div');
    title.className='position-name';
    title.textContent=row.instrument;
    const grid=document.createElement('div');
    grid.className='position-grid';
    const fields=[
      ['持仓量（USDT）',usdt(row.notional_usdt)],
      ['保证金（USDT）',usdt(row.margin_usdt)],
      ['维持保证金率',pct(row.maintenance_margin_ratio_pct)],
      ['开仓均价',num(row.average)],
      ['标记价格',num(row.mark)],
      ['策略止损价',num(row.strategy_stop)],
    ];
    fields.forEach(([label,value])=>{
      const cell=document.createElement('div');
      cell.className='position-field';
      const name=document.createElement('span');
      name.textContent=label;
      const strong=document.createElement('strong');
      strong.textContent=value;
      cell.append(name,strong);
      grid.append(cell);
    });
    card.append(title,grid);
    return card;
  }));
  $('empty-markets').hidden = positions.length > 0;
  renderManualEntry(s);
  renderEntryCandidates(s);
  const logText = s.logs.map(line=>`[${date(line.time)}] ${line.text}`).join('\n') || '等待启动策略，运行信息会显示在这里。';
  const follow = $('logs').scrollHeight - $('logs').scrollTop - $('logs').clientHeight < 40;
  if ($('logs').textContent !== logText) { $('logs').textContent=logText; if(follow) $('logs').scrollTop=$('logs').scrollHeight; }
  $('log-count').textContent = s.logs.length + ' 条';
  syncSettings(s); controls();
}
function manualInstrumentMatch(value) {
  const text=String(value || '').trim();
  if (!text) return null;
  if (selectedManualInstrument === text) return text;
  return marketCatalog.some(item=>item.instrument === text) ? text : null;
}

function manualInstrumentSuggestions(value) {
  const query=String(value || '').trim().toUpperCase();
  if (!query) return [];
  return marketCatalog
    .filter(item=>{
      const instrument=item.instrument.toUpperCase();
      const base=instrument.split('-')[0];
      return base.startsWith(query) || instrument.includes(query);
    })
    .slice(0,8);
}

function renderManualSuggestions() {
  const input=$('manual-entry-instrument');
  const panel=$('manual-entry-suggestions');
  const matches=manualInstrumentSuggestions(input.value);
  panel.replaceChildren(...matches.map(item=>{
    const button=document.createElement('button');
    button.type='button';
    button.className='manual-entry-suggestion';
    button.setAttribute('role','option');
    button.textContent=item.instrument;
    button.addEventListener('mousedown',event=>event.preventDefault());
    button.addEventListener('click',()=>{
      input.value=item.instrument;
      selectedManualInstrument=item.instrument;
      panel.hidden=true;
      input.setAttribute('aria-expanded','false');
      controls();
      input.focus();
    });
    return button;
  }));
  panel.hidden=!matches.length;
  input.setAttribute('aria-expanded',matches.length ? 'true' : 'false');
}

function renderManualEntry(s) {
  if (s.manual_entry_pending) {
    $('manual-entry-note').textContent = '手动开仓请求已提交，worker 正在核验最新 K 线、初始止损、当前最优卖价、账户余额和交易所最小张数。';
  } else if (!s.running) {
    $('manual-entry-note').textContent = '启动模拟交易或实盘交易后，输入币种并从联想结果选择可交易合约。';
  } else if (!s.manual_entry_enabled) {
    $('manual-entry-note').textContent = '当前状态不能手动开仓，请先处理异常暂停、待核对订单或等待策略准备完成。';
  } else if (catalogLoading) {
    $('manual-entry-note').textContent = '正在加载当前可交易 USDT 永续合约，用于输入联想。';
  } else {
    $('manual-entry-note').textContent = '输入 BTC 等简称会显示匹配的当前可交易合约；不会自动改写输入内容。选择联想项后再按资金比例提交。';
  }
}

async function submitManualEntry() {
  const instrument = manualInstrumentMatch($('manual-entry-instrument').value);
  const percent = Number($('manual-entry-fraction').value);
  if (
    !instrument ||
    !Number.isFinite(percent) ||
    percent <= 0 ||
    percent > 100 ||
    actionBusy ||
    last?.manual_entry_pending
  ) return;
  actionBusy = true;
  $('manual-entry-note').textContent = `正在提交 ${instrument}，本次首仓使用 ${percent}% 策略资金…`;
  controls();
  try {
    await api('/api/manual-entry', {
      mode: $('mode').value,
      instrument,
      capital_fraction: percent / 100,
    });
    $('manual-entry-note').textContent = `${instrument} 已提交（${percent}%），worker 将按当前最优卖价限价核验后提交。`;
  } catch (error) {
    $('manual-entry-note').textContent = '手动开仓失败：' + error.message;
  } finally {
    actionBusy = false;
    await refresh();
    controls();
  }
}

function renderEntryCandidates(s) {
  const candidates = s.entry_candidates || [];
  $('candidate-count').textContent = candidates.length + ' 个候选';
  $('empty-candidates').hidden = candidates.length > 0;
  $('candidate-body').replaceChildren(...candidates.map(candidate => {
    const tr = document.createElement('tr');
    const instrumentCell = document.createElement('td');
    instrumentCell.textContent = candidate.instrument;
    const volumeCell = document.createElement('td');
    const volume = Number(candidate.volume_usdt_24h);
    volumeCell.textContent = Number.isFinite(volume)
      ? volume.toLocaleString('zh-CN', {maximumFractionDigits: 0}) + ' USDT'
      : '—';
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'candidate-open';
    button.textContent = '开仓';
    button.disabled = !s.entry_approval_enabled || actionBusy;
    button.addEventListener('click', () => approveEntry(candidate.id, candidate.instrument));
    actionCell.append(button);
    tr.append(instrumentCell, volumeCell, actionCell);
    return tr;
  }));
  if (s.entry_candidate_error) {
    $('candidate-feedback').textContent = '候选列表读取失败：' + s.entry_candidate_error;
  } else if (!s.running) {
    $('candidate-feedback').textContent = '启动模拟交易或实盘交易后，点击“生成候选”进行扫描。';
  } else if (s.candidate_scan_pending) {
    $('candidate-feedback').textContent = '正在扫描交易所全部受支持的 USDT 永续合约…';
  } else if (!s.entry_approval_enabled && candidates.length) {
    $('candidate-feedback').textContent = '当前状态暂不能开仓，请先处理异常暂停或待核对订单。';
  } else if (!$('candidate-feedback').dataset.locked) {
    $('candidate-feedback').textContent = candidates.length
      ? '候选按 24h USDT 成交量从高到低排列；点击“开仓”时会按最新行情重新核验并计算止损和张数。'
      : '不会因新 K 线自动扫描；需要时点击“生成候选”。';
  }
}

async function generateCandidates() {
  if (actionBusy || last?.candidate_scan_pending) return;
  actionBusy = true;
  $('candidate-feedback').dataset.locked = '1';
  $('candidate-feedback').textContent = '正在提交候选扫描…';
  controls();
  try {
    await api('/api/generate-candidates', {mode: $('mode').value});
    $('candidate-feedback').textContent = '扫描请求已提交，等待策略返回结果…';
  } catch (error) {
    $('candidate-feedback').textContent = '生成候选失败：' + error.message;
  } finally {
    actionBusy = false;
    await refresh();
    delete $('candidate-feedback').dataset.locked;
    controls();
  }
}

async function approveEntry(candidateId, instrument) {
  if (actionBusy) return;
  actionBusy = true;
  $('candidate-feedback').dataset.locked = '1';
  $('candidate-feedback').textContent = '正在提交 ' + instrument + ' 开仓确认…';
  controls();
  try {
    await api('/api/approve-entry', {
      mode: $('mode').value,
      candidate_id: candidateId,
    });
    $('candidate-feedback').textContent = instrument + ' 已提交，策略将在下一轮重新核验后执行。';
  } catch (error) {
    $('candidate-feedback').textContent = '开仓确认失败：' + error.message;
  } finally {
    actionBusy = false;
    await refresh();
    delete $('candidate-feedback').dataset.locked;
    controls();
  }
}

function renderNetwork(status) {
  const node = $('okx-network');
  if (status.connected) {
    node.textContent = '已连接 · ' + Math.round(status.latency_ms) + ' ms';
    node.className = 'network-status ok';
    const skew = Number(status.clock_skew_ms);
    node.title = Number.isFinite(skew) ? 'OKX 服务器时钟偏差 ' + skew + ' ms' : '';
  } else {
    node.textContent = '连接异常';
    node.className = 'network-status bad';
    node.title = status.error || '';
  }
  $('okx-network-time').textContent = status.checked_at ? date(status.checked_at) : '—';
}

async function refreshNetwork() {
  if (networkLoading) return;
  networkLoading = true;
  const node = $('okx-network');
  const button = $('refresh-network');
  node.textContent = '正在检测…';
  node.className = 'network-status checking';
  node.title = '';
  button.disabled = true;
  button.textContent = '检测中…';
  try {
    renderNetwork(await api('/api/connectivity'));
  } catch (error) {
    node.textContent = '网页连接异常';
    node.className = 'network-status bad';
    node.title = error.message;
    $('okx-network-time').textContent = date(Date.now() / 1000);
  } finally {
    networkLoading = false;
    button.disabled = false;
    button.textContent = '刷新';
  }
}

async function refresh() {
  if (loading) return; loading=true;
  try { const s=await api('/api/status?mode='+encodeURIComponent($('mode').value)); connected=true; $('connection').textContent='服务已连接 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false}); render(s);
    if (s.credentials[s.profile] && !s.checking && !checkBusy && Date.now() - Math.max(accountAttempts[s.profile] || 0, (s.account?.time || 0)*1000, (s.account?.error_time || 0)*1000) > 60000) void checkAccount();
    if (catalogProfile !== s.profile && !catalogLoading) void loadInstrumentCatalog();
  }
  catch(error) {connected=false; $('connection').textContent='连接中断 · 请检查网页服务'; message(error.message); controls();}
  finally {loading=false;}
}
async function action(path,data) {
  actionBusy=true; message(''); controls();
  try {await api(path,data);} catch(error){message(error.message);}
  finally {actionBusy=false; await refresh(); controls();}
}
$('start').addEventListener('click',()=>action('/api/start',{mode:$('mode').value,confirm_live:$('mode').value==='live'}));
$('stop').addEventListener('click',()=>action('/api/stop',{}));
async function checkAccount() {
  if (checkBusy || last?.checking) return;
  checkBusy=true; accountAttempts[last?.profile || 'live']=Date.now(); controls();
  try {await api('/api/check',{mode:$('mode').value});}
  catch(error){message(error.message);}
  finally {checkBusy=false; await refresh(); controls();}
}
async function loadInstrumentCatalog() {
  if (catalogLoading) return;
  catalogLoading=true;
  const mode=$('mode').value;
  const profile=last?.profile;
  try {
    const data=await api('/api/instruments?mode='+encodeURIComponent(mode));
    if (last?.profile !== profile) return;
    marketCatalog=Array.isArray(data.items) ? data.items : [];
    catalogProfile=profile;
    renderManualSuggestions();
  } catch(error) {
    marketCatalog=[];
    catalogProfile=profile;
    if (last && !last.manual_entry_pending) {
      $('manual-entry-note').textContent='币种联想目录加载失败：'+error.message;
    }
  } finally {
    catalogLoading=false;
    if (last) renderManualEntry(last);
    controls();
  }
}
$('check').addEventListener('click',()=>{message('');checkAccount();});
$('refresh-network').addEventListener('click',()=>refreshNetwork());
$('generate-candidates').addEventListener('click',()=>generateCandidates());
$('manual-entry-instrument').addEventListener('input',()=>{
  selectedManualInstrument=null;
  renderManualSuggestions();
  controls();
});
$('manual-entry-instrument').addEventListener('focus',()=>renderManualSuggestions());
$('manual-entry-instrument').addEventListener('blur',()=>{
  setTimeout(()=>{
    $('manual-entry-suggestions').hidden=true;
    $('manual-entry-instrument').setAttribute('aria-expanded','false');
  },100);
});
$('manual-entry-fraction').addEventListener('input',()=>controls());
$('manual-entry-submit').addEventListener('click',()=>submitManualEntry());
$('mode').addEventListener('change',()=>{
  message('');
  cancelSettingsEdit();
  marketCatalog=[];
  catalogProfile=null;
  selectedManualInstrument=null;
  $('manual-entry-suggestions').hidden=true;
  controls();
  refresh();
});
controls(); refresh(); refreshNetwork(); setInterval(refresh,2000);

function settingsLocked() {
  return !!last?.settings_locked || !!last?.external_process?.length || checkBusy || !!last?.checking || settingsSaving;
}

function selectEditableText(node) {
  const selection=window.getSelection();
  const range=document.createRange();
  range.selectNodeContents(node);
  selection.removeAllRanges();
  selection.addRange(range);
}

function setEditable(node, enabled) {
  node.contentEditable = enabled ? 'true' : 'false';
  node.classList.toggle('inline-editing', enabled);
  node.setAttribute('aria-label', enabled ? '正在编辑' : '');
}

function restoreSettingsText() {
  if (!last) return;
  $('allocation').replaceChildren(
    document.createTextNode(Math.round(last.config.capital_fraction*10000)/100),
    Object.assign(document.createElement('span'),{textContent:'%'})
  );
  $('bar').textContent=last.config.bar;
  $('leverage').textContent=last.config.leverage+'×';
}

function cancelSettingsEdit() {
  setEditable($('allocation'), false);
  setEditable($('bar'), false);
  setEditable($('leverage'), false);
  settingsEditor=null;
  restoreSettingsText();
  controls();
}

function beginSettingsEdit(kind) {
  if (!last || settingsLocked()) return;
  if (settingsEditor && settingsEditor !== kind) cancelSettingsEdit();
  settingsEditor=kind;
  settingsProfile=last.profile;
  message('');
  if (kind === 'capital') {
    $('allocation').textContent=String(Math.round(last.config.capital_fraction*10000)/100);
    setEditable($('allocation'), true);
    $('allocation').focus();
    selectEditableText($('allocation'));
  } else {
    $('bar').textContent=last.config.bar;
    $('leverage').textContent=String(last.config.leverage);
    setEditable($('bar'), true);
    setEditable($('leverage'), true);
    $('bar').focus();
    selectEditableText($('bar'));
  }
  controls();
}

function parseCapital() {
  const value=Number($('allocation').textContent.trim().replace('%','').replace(',','.'));
  if (!Number.isFinite(value) || value <= 0 || value > 100) {
    throw new Error('资金使用上限必须大于 0% 且不超过 100%');
  }
  return value / 100;
}

function parseStrategyDisplay() {
  const bar=$('bar').textContent.trim();
  const leverage=Number($('leverage').textContent.trim().replace(/[×xX倍]/g,''));
  const barField=last?.parameters?.fields?.find(field=>field.key==='bar');
  const choices=barField?.choices || [];
  if (!choices.includes(bar)) {
    throw new Error('K 线周期无效，可用值：'+choices.join('、'));
  }
  if (!Number.isInteger(leverage) || leverage < 1 || leverage > 125) {
    throw new Error('逐仓杠杆必须是 1–125 的整数');
  }
  return {bar, leverage};
}

async function saveSettingsEdit() {
  if (!settingsEditor || settingsSaving || settingsLocked()) return;
  let live;
  try {
    live = settingsEditor === 'capital'
      ? {capital_fraction: parseCapital()}
      : parseStrategyDisplay();
  } catch(error) {
    message(error.message);
    return;
  }
  settingsSaving=true;
  message('');
  controls();
  try {
    await api('/api/settings',{mode:$('mode').value,values:{live,strategy:{}}});
    const finished=settingsEditor;
    setEditable($('allocation'), false);
    setEditable($('bar'), false);
    setEditable($('leverage'), false);
    settingsEditor=null;
    await refresh();
    message('');
    if (finished === 'capital') $('edit-capital').focus(); else $('edit-strategy').focus();
  } catch(error) {
    message(error.message);
  } finally {
    settingsSaving=false;
    controls();
  }
}

function handleSettingButton(kind) {
  if (settingsEditor === kind) void saveSettingsEdit();
  else beginSettingsEdit(kind);
}

$('edit-capital').addEventListener('click',()=>handleSettingButton('capital'));
$('edit-strategy').addEventListener('click',()=>handleSettingButton('strategy'));

for (const node of [$('allocation'),$('bar'),$('leverage')]) {
  node.addEventListener('keydown',event=>{
    if (!settingsEditor) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      cancelSettingsEdit();
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      void saveSettingsEdit();
    }
  });
  node.addEventListener('paste',event=>{
    if (!settingsEditor) return;
    event.preventDefault();
    const text=(event.clipboardData || window.clipboardData).getData('text/plain');
    document.execCommand('insertText',false,text.replace(/[\r\n]/g,''));
  });
}

function syncSettings(s) {
  if(settingsProfile!==s.profile){
    settingsProfile=s.profile;
    if(settingsEditor) cancelSettingsEdit();
  }
  if(settingsEditor && s.settings_locked) cancelSettingsEdit();
  controls();
}

