'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="panel-token"]').content;
const labels = {watch:'只读观察', 'demo-watch':'模拟账户观察', demo:'模拟交易', live:'实盘交易'};
let last = null, actionBusy = false, checkBusy = false, loading = false, connected = false;
const date = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN', {hour12:false}) : '—';
const num = value => value === null || value === undefined ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits:6});
async function api(path, data) {
  const response = await fetch(path, {cache:'no-store', headers:{'X-Panel-Token':token, ...(data ? {'Content-Type':'application/json'} : {})}, ...(data ? {method:'POST', body:JSON.stringify(data)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function message(text) { text = text.replace('missing OKX environment variables: ', '尚未配置完整的环境变量：'); $('error').textContent = text; $('error').hidden = !text; }
function controls() {
  const mode = $('mode').value;
  const active = last?.running;
  $('mode').disabled = !!active || actionBusy || checkBusy;
  $('start').disabled = !connected || !!active || actionBusy || checkBusy || !!last?.external_process?.length;
  $('stop').disabled = !connected || !active || !!last?.stopping || actionBusy;
  $('check').disabled = !connected || checkBusy || !!last?.checking;
  $('check').textContent = checkBusy || last?.checking ? '正在检查…' : '检查账户 ↗';
  $('start').textContent = actionBusy ? '处理中…' : mode === 'live' ? '▶ 启动实盘交易' : mode === 'demo' ? '▶ 启动模拟交易' : '▶ 启动观察';
  $('start').classList.toggle('live', mode === 'live');
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
  if (s.account?.error) warnings.push('最近账户检查失败：' + s.account.error);
  $('warning').textContent = warnings.join(' '); $('warning').hidden = !warnings.length;
  const phase = s.stopping ? '正在停止' : s.running ? (s.halt ? '异常暂停' : beat?.phase === 'error' ? '检查失败' : beat?.phase === 'ready' ? labels[s.mode] + '运行中' : '正在启动') : s.external_process.length ? '外部进程运行中' : s.exit_code !== null && s.exit_code !== 0 ? '进程已退出' : '未运行';
  $('run-badge').textContent = phase;
  $('run-badge').className = 'badge ' + (s.halt || (s.exit_code && !s.running) ? 'danger' : s.running ? '' : 'neutral');
  $('profile').textContent = s.profile === 'demo' ? '模拟账户' : '实盘账户';
  $('keys').textContent = s.credentials[s.profile] ? '已配置 · 环境变量' : '未配置完整';
  $('key-note').textContent = s.credentials[s.profile] ? '密钥仅从环境变量读取，不在网页中显示。' : '请设置 ' + ['API_KEY','API_SECRET','API_PASSPHRASE'].map(name => (s.profile === 'demo' ? 'OKX_DEMO_' : 'OKX_') + name).join('、') + ' 后，重新启动网页服务。';
  $('started').textContent = s.started_at ? date(s.started_at) : '尚未启动';
  $('heartbeat').textContent = beat ? date(beat.updated_at) : '—';
  $('bar').textContent = s.config.bar; $('leverage').textContent = s.config.leverage + '×';
  $('allocation').replaceChildren(document.createTextNode(Math.round(s.config.capital_fraction*100)), Object.assign(document.createElement('span'),{textContent:'%'}));
  const account = s.account?.data;
  $('equity').textContent = num(account?.account_equity_usdt_equivalent);
  $('available').textContent = num(account?.available_usdt);
  $('account-time').textContent = s.account?.time ? '检查于 ' + date(s.account.time) : '点击“检查账户”读取';
  $('account-orders').textContent = account ? `${account.open_positions} / ${account.pending_orders}` : '—';
  $('budget').textContent = account ? '当前权益对应上限 ' + num(account.capital_limit_usdt_approx) + ' USDT' : '保证金与手续费预留';
  $('position-count').replaceChildren(document.createTextNode(s.markets.filter(m => Number(m.contracts)>0).length), Object.assign(document.createElement('span'), {textContent:' 个币对'}));
  $('pending').textContent = s.pending ? '有待核对订单' : '本地策略记录 · 无待核对订单';
  const rows = s.markets.length ? s.markets : (account?.instruments || []).map(instrument=>({instrument, contracts:null, legs:null}));
  $('market-count').textContent = rows.length ? `${rows.length} 个币对${s.markets.length ? '' : ' · 只读候选'}` : `首次启动筛选前 ${s.config.top_n} 个`;
  $('market-body').replaceChildren(...rows.map(row=>{
    const tr = document.createElement('tr');
    [row.instrument,num(row.contracts),num(row.average),num(row.stop),row.legs ?? '—',date(row.last_bar)].forEach(value=>{const td=document.createElement('td');td.textContent=value;tr.append(td);});return tr;
  }));
  $('empty-markets').hidden = rows.length > 0;
  const logText = s.logs.map(line=>`[${date(line.time)}] ${line.text}`).join('\n') || '等待启动策略，运行信息会显示在这里。';
  const follow = $('logs').scrollHeight - $('logs').scrollTop - $('logs').clientHeight < 40;
  if ($('logs').textContent !== logText) { $('logs').textContent=logText; if(follow) $('logs').scrollTop=$('logs').scrollHeight; }
  $('log-count').textContent = s.logs.length + ' 条'; controls();
}
async function refresh() {
  if (loading) return; loading=true;
  try { const s=await api('/api/status?mode='+encodeURIComponent($('mode').value)); connected=true; $('connection').textContent='本机已连接 · '+new Date().toLocaleTimeString('zh-CN',{hour12:false}); render(s); }
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
$('check').addEventListener('click',async()=>{
  checkBusy=true; message(''); controls();
  try {await api('/api/check',{mode:$('mode').value});} catch(error){message(error.message);}
  finally {checkBusy=false; await refresh(); controls();}
});
$('mode').addEventListener('change',()=>{message('');controls();refresh();});
controls(); refresh(); setInterval(refresh,2000);
