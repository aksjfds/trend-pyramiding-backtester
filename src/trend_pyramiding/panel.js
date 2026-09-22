'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="panel-token"]').content;
const labels = {watch:'只读观察', 'demo-watch':'模拟账户观察', demo:'模拟交易', live:'实盘交易'};
let marketCatalog = [], catalogProfile = null, catalogLoading = false;
let settingsDirty=false, settingsSaving=false, settingsProfile=null, settingsFields=[];
const accountAttempts = {};
let last = null, actionBusy = false, checkBusy = false, loading = false, connected = false, networkLoading = false;
const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', {hour12:false}) : '—';
const num = value => value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:6});
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
  $('start').disabled = !connected || !!active || actionBusy || checkBusy || !!last?.external_process?.length || settingsDirty || settingsSaving;
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
    !$('manual-entry-instrument').value ||
    !manualFractionValid;
  $('manual-entry-submit').textContent =
    last?.manual_entry_pending ? '提交中…' : '开仓并交给策略接管';
  $('start').textContent = actionBusy ? '处理中…' : mode === 'live' ? '▶ 启动实盘交易' : mode === 'demo' ? '▶ 启动模拟交易' : '▶ 启动观察';
  $('start').classList.toggle('live', mode === 'live');
  const settingsLocked = !!last?.settings_locked || !!last?.external_process?.length || checkBusy || !!last?.checking || settingsSaving;
  document.querySelectorAll('.settings-save').forEach(button=>{
    button.disabled = !connected || settingsLocked || !settingsDirty;
    button.textContent = settingsSaving ? '正在保存…' : (button.closest('.capital-metric') ? '保存' : '保存参数');
  });
  document.querySelectorAll('.settings-reset').forEach(button=>{
    button.disabled = settingsSaving || !settingsDirty;
  });
  document.querySelectorAll('#settings-form input,#settings-form select').forEach(input=>input.disabled=settingsLocked);
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
  $('bar').textContent = s.config.bar; $('leverage').textContent = s.config.leverage + '×';
  $('allocation').replaceChildren(document.createTextNode(Math.round(s.config.capital_fraction*10000)/100), Object.assign(document.createElement('span'),{textContent:'%'}));
  const account = s.account?.data;
  $('equity').textContent = num(account?.account_equity_usdt_equivalent);
  $('available').textContent = num(account?.available_usdt);
  $('account-time').textContent = s.account?.time ? '更新于 ' + date(s.account.time) : s.credentials[s.profile] ? '正在读取账户…' : '尚未配置此账户';
  $('account-orders').textContent = account ? `${account.open_positions ?? "—"} / ${account.pending_orders ?? "—"}` : '—';
  $('budget').textContent = account ? '当前权益对应上限 ' + num(account.capital_limit_usdt_approx) + ' USDT' : '保证金与手续费预留';
  $('position-count').replaceChildren(document.createTextNode(s.markets.filter(m => Number(m.contracts)>0).length), Object.assign(document.createElement('span'), {textContent:' 个币对'}));
  $('pending').textContent = s.pending ? '有待核对订单' : '策略保存的记录 · 无待核对订单';
  const rows = s.markets || [];
  $('market-count').textContent = rows.length ? `${rows.length} 个策略管理币对` : '当前没有策略管理币对';
  $('market-body').replaceChildren(...rows.map(row=>{
    const tr = document.createElement('tr');
    [row.instrument,num(row.contracts),num(row.average),num(row.stop),row.legs ?? '—',date(row.last_bar)].forEach(value=>{const td=document.createElement('td');td.textContent=value;tr.append(td);});return tr;
  }));
  $('empty-markets').hidden = rows.length > 0;
  renderManualEntry(s);
  renderEntryCandidates(s);
  const logText = s.logs.map(line=>`[${date(line.time)}] ${line.text}`).join('\n') || '等待启动策略，运行信息会显示在这里。';
  const follow = $('logs').scrollHeight - $('logs').scrollTop - $('logs').clientHeight < 40;
  if ($('logs').textContent !== logText) { $('logs').textContent=logText; if(follow) $('logs').scrollTop=$('logs').scrollHeight; }
  $('log-count').textContent = s.logs.length + ' 条';
  syncSettings(s); controls();
}
function renderManualEntry(s) {
  const select = $('manual-entry-instrument');
  const previous = select.value;
  const held = new Set(
    (s.markets || [])
      .filter(row => Number(row.contracts || 0) > 0)
      .map(row => row.instrument)
  );
  const managed = (s.manual_entry_instruments || []).filter(instrument => !held.has(instrument));
  const catalog = marketCatalog.map(item => item.instrument).filter(instrument => !held.has(instrument));
  const instruments = [...managed, ...catalog.filter(instrument => !managed.includes(instrument))];
  select.replaceChildren(Object.assign(document.createElement('option'), {
    value: '',
    textContent: instruments.length ? '选择可交易币种' : '正在加载可交易币种…',
  }));
  instruments.forEach(instrument => {
    const option = document.createElement('option');
    option.value = instrument;
    option.textContent = instrument;
    select.append(option);
  });
  if (instruments.includes(previous)) select.value = previous;

  if (s.manual_entry_pending) {
    $('manual-entry-note').textContent = '手动开仓请求已提交，worker 正在核验最新 K 线、初始止损、当前最优卖价、账户余额和交易所最小张数。';
  } else if (!s.running) {
    $('manual-entry-note').textContent = '启动模拟交易或实盘交易后，选择币种和资金使用比例即可手动开仓。';
  } else if (!s.manual_entry_enabled) {
    $('manual-entry-note').textContent = '当前状态不能手动开仓，请先处理异常暂停、待核对订单或等待策略准备完成。';
  } else {
    $('manual-entry-note').textContent = '不要求进入候选列表；本次首仓按所选资金比例计算张数，并以当前最优卖价作为买入限价提交 FOK，不按市价追单。成交后由策略接管加仓、止损和退出。';
  }
}

async function submitManualEntry() {
  const instrument = $('manual-entry-instrument').value;
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
  const now = Date.now() / 1000;
  $('candidate-count').textContent = candidates.length + ' 个候选';
  $('empty-candidates').hidden = candidates.length > 0;
  $('candidate-body').replaceChildren(...candidates.map(candidate => {
    const tr = document.createElement('tr');
    const remaining = Math.max(0, Math.ceil(Number(candidate.expires_at) - now));
    const values = [
      candidate.instrument,
      date(candidate.bar),
      num(candidate.signal_close),
      num(candidate.stop),
      candidate.indicative_contracts,
      remaining > 0 ? remaining + ' 秒' : '已失效',
    ];
    values.forEach(value => {
      const td = document.createElement('td');
      td.textContent = value;
      tr.append(td);
    });
    const actionCell = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'candidate-open';
    button.textContent = '开仓';
    button.disabled = !s.entry_approval_enabled || remaining <= 0 || actionBusy;
    button.addEventListener('click', () => approveEntry(candidate.id, candidate.instrument));
    actionCell.append(button);
    tr.append(actionCell);
    return tr;
  }));
  if (s.entry_candidate_error) {
    $('candidate-feedback').textContent = '候选列表读取失败：' + s.entry_candidate_error;
  } else if (!s.running) {
    $('candidate-feedback').textContent = '启动模拟交易或实盘交易后，点击“生成候选”进行扫描。';
  } else if (s.candidate_scan_pending) {
    $('candidate-feedback').textContent = '正在扫描交易所全部受支持的 USDT 永续合约…';
  } else if (!s.entry_approval_enabled && candidates.length) {
    $('candidate-feedback').textContent = '当前状态暂不能批准新开仓，请先处理异常暂停或待核对订单。';
  } else if (!$('candidate-feedback').dataset.locked) {
    $('candidate-feedback').textContent = candidates.length
      ? '本次扫描候选只在当前有效期内可确认。'
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
    if (catalogProfile !== s.profile && !catalogLoading) void loadMarkets();
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
async function loadMarkets() {
  if (catalogLoading) return; catalogLoading=true;
  const mode=$('mode').value, profile=last?.profile;
  try {
    const data=await api('/api/instruments?mode='+encodeURIComponent(mode));
    if (last?.profile !== profile) return;
    marketCatalog=data.items; catalogProfile=profile;
    if (last) renderManualEntry(last);
    controls();
  } catch(error) {
    catalogProfile=profile;
    if (last && !last.manual_entry_pending) {
      $('manual-entry-note').textContent='可交易币种加载失败：'+error.message;
    }
  } finally {catalogLoading=false;}
}
$('check').addEventListener('click',()=>{message('');checkAccount();});
$('refresh-network').addEventListener('click',()=>refreshNetwork());
$('generate-candidates').addEventListener('click',()=>generateCandidates());
$('manual-entry-instrument').addEventListener('change',()=>controls());
$('manual-entry-fraction').addEventListener('input',()=>controls());
$('manual-entry-submit').addEventListener('click',()=>submitManualEntry());
$('mode').addEventListener('change',()=>{message('');settingsDirty=false;marketCatalog=[];catalogProfile=null;controls();refresh();});
controls(); refresh(); refreshNetwork(); setInterval(refresh,2000);

function setSettingsStatus(note, feedback='') {
  document.querySelectorAll('.settings-note').forEach(node=>node.textContent=note);
  document.querySelectorAll('.settings-feedback').forEach(node=>node.textContent=feedback);
}

function syncSettings(s) {
  if (!s.parameters) return;
  if(settingsProfile!==s.profile){settingsDirty=false;settingsProfile=s.profile;}
  if(!settingsFields.length){
    settingsFields=s.parameters.fields;
    for(const field of settingsFields){
      const label=document.createElement('label'); label.textContent=field.label;
      const input=document.createElement(field.type==='select'?'select':'input');
      input.id='setting-'+field.key; label.htmlFor=input.id;
      if(field.type==='select'){
        for(const value of field.choices){const option=document.createElement('option');option.value=value;option.textContent=value.replace('utc','（UTC）');input.append(option);}
      }else{
        input.type=field.type==='weights'?'text':field.type;
        if(field.type==='number'){input.min=field.min;input.max=field.max;input.step=field.step;input.required=true;}
        if(field.type==='weights'){input.placeholder='30, 30, 20, 20';input.required=true;}
      }
      input.addEventListener('input',()=>{
        settingsDirty=true;
        setSettingsStatus('参数尚未保存，保存后下次启动生效。');
        controls();
      });
      input.addEventListener('change',()=>{
        settingsDirty=true;
        setSettingsStatus('参数尚未保存，保存后下次启动生效。');
        controls();
      });
      label.append(input);
      const target = field.key === 'capital_fraction'
        ? $('capital-settings')
        : field.section === 'live'
          ? $('strategy-basic-settings')
          : $('advanced-settings');
      target.append(label);
    }
  }
  if(!settingsDirty&&!settingsSaving){
    for(const field of settingsFields){
      const input=$('setting-'+field.key),value=s.parameters.values[field.section][field.key];
      if(field.type==='checkbox')input.checked=value;
      else if(field.type==='weights')input.value=value.map(x=>Number((x*100).toFixed(8))).join(', ');
      else input.value=typeof value==='number'?Number((value*(field.scale||1)).toFixed(8)):value;
    }
  }
  setSettingsStatus(
    s.settings_locked
      ? '运行中或仍有持仓、待核对订单时，暂时不能修改参数。'
      : settingsDirty
        ? '参数尚未保存，保存后下次启动生效。'
        : '设置分别保存到当前实盘或模拟账户；杠杆上限还需通过交易所核验。'
  );
}

document.querySelectorAll('.settings-reset').forEach(button=>button.addEventListener('click',()=>{
  settingsDirty=false;
  syncSettings(last);
  controls();
  setSettingsStatus(
    last?.settings_locked
      ? '运行中或仍有持仓、待核对订单时，暂时不能修改参数。'
      : '设置分别保存到当前实盘或模拟账户；杠杆上限还需通过交易所核验。',
    '已恢复到保存的参数。'
  );
}));

$('settings-form').addEventListener('submit',async event=>{
  event.preventDefault(); if(!$('settings-form').reportValidity())return;
  const values={live:{},strategy:{}};
  for(const field of settingsFields){
    const input=$('setting-'+field.key);
    values[field.section][field.key]=field.type==='checkbox'?input.checked:field.type==='weights'?input.value.split(/[,，]/).map(x=>Number(x.trim())/100):field.type==='number'?Number(input.value)/(field.scale||1):input.value;
  }
  settingsSaving=true;message('');controls();
  try{
    await api('/api/settings',{mode:$('mode').value,values});
    settingsDirty=false;
    setSettingsStatus('设置分别保存到当前实盘或模拟账户；杠杆上限还需通过交易所核验。','参数已保存，下次启动生效。');
  }
  catch(error){message(error.message);}
  finally{settingsSaving=false;await refresh();controls();}
});
