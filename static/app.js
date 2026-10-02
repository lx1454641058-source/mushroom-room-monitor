'use strict';
const $ = id => document.getElementById(id);
let configLoaded = false, mode = '', rows = [], stream = null, photoTimer = null, polling = false, toastTimer;
let controlBusy = false, latestStatus = null, displayedBoot = null;
const dateText = value => new Date(value).toLocaleString('zh-CN', {hour12: false});
const show = (message, error = false) => {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').className = error ? 'toast error' : 'toast'; $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 10000 : 5000);
};
async function api(path, body, mime = 'application/json') {
  const options = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': mime, 'X-Room-Client': 'local-dashboard'}, body: mime === 'application/json' ? JSON.stringify(body) : body};
  const response = await fetch(path, {...options, signal: AbortSignal.timeout(path === '/api/vision' ? 135000 : path === '/api/analysis' || path === '/api/agent' ? 55000 : 8000)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}
function markOffline(message) {
  latestStatus = null;
  $('connection').textContent = '连接中断';
  $('temperature').textContent = $('humidity').textContent = '—';
  $('mist').textContent = $('fan').textContent = '未知';
  $('water').textContent = '水位状态：未知'; $('enabled').textContent = '状态未知';
  $('fanMode').textContent = '通信中断，通风模式未知';
  $('liquidState').textContent = '通信中断，泵输出状态未知'; $('liquidLevels').textContent = '';
  for (const button of document.querySelectorAll('[data-liquid],[data-liquid-stop]')) button.disabled = true;
  $('fault').textContent = message;
  for (const button of document.querySelectorAll('#start,#stop,#save,[data-fan]')) button.disabled = true;
  renderClimate(null);
}
function render(value) {
  latestStatus = value;
  mode = value.mode;
  const demo = mode === 'demo', s = value.status;
  $('mode').textContent = demo ? '模拟演示' : '实物串口';
  $('sourceNote').textContent = demo ? '当前为模拟数据，用于验证系统功能与答辩演示，不代表真实蘑菇房环境。' : '当前为串口采集。先核对硬件接线与设备额定参数，再启用控制。';
  $('sourceNote').className = demo ? 'notice' : 'notice live';
  $('demoPanel').hidden = !demo;
  $('aiButton').hidden = !value.model; $('aiNote').hidden = !value.model;
  $('agentEngine').textContent = value.model ? '本地模型 + 工具执行' : '常用指令模式';
  $('visionEngine').textContent = value.vision_model || '未配置视觉模型';
  renderClimate(value.connected ? s.climate : null, s.boot_id);
  if (!value.connected) { markOffline(value.error || '数据过期，当前状态未知'); }
  else {
    $('connection').textContent = '● 通信正常';
    $('temperature').textContent = s.temperature === null ? '—' : s.temperature.toFixed(1);
    $('humidity').textContent = s.humidity === null ? '—' : s.humidity.toFixed(1);
    $('mist').textContent = s.mist ? '正在加湿' : '已关闭';
    $('fan').textContent = s.fan ? '正在通风' : '已关闭';
    $('water').textContent = s.water_ok ? '水位正常' : '缺水 / 水位开关断线';
    $('enabled').textContent = s.enabled ? '控制已启用' : '已停止';
    const liquid = s.liquids;
    if (liquid) {
      const running = liquid.water_pump || liquid.nutrient_pump;
      $('liquidState').textContent = '清水泵：' + (liquid.water_pump ? '运行' : '停止') + ' · 营养液泵：' + (liquid.nutrient_pump ? '运行' : '停止') + ' · 剩余 ' + (liquid.remaining_ms / 1000).toFixed(1) + ' 秒 · ' + liquid.fault;
      $('liquidLevels').textContent = '清水储罐：' + (liquid.water_source_ok ? '足液' : '缺液/断线') + '；雾化水箱：' + (liquid.water_room_ok ? '允许补水' : '已满/断线') + '；营养液储罐：' + (liquid.nutrient_source_ok ? '足液' : '缺液/断线') + '；接收容器：' + (liquid.nutrient_room_ok ? '可接收' : '已满/断线');
      for (const button of document.querySelectorAll('[data-liquid]')) {
        const pump = button.dataset.liquid;
        button.disabled = controlBusy || s.enabled || running || !liquid[pump + '_source_ok'] || !liquid[pump + '_room_ok'];
      }
      for (const button of document.querySelectorAll('[data-liquid-stop]')) button.disabled = controlBusy || !liquid[button.dataset.liquidStop + '_pump'];
    } else {
      $('liquidState').textContent = '设备未报告液体模块，需要更新配套固件'; $('liquidLevels').textContent = '';
      for (const button of document.querySelectorAll('[data-liquid],[data-liquid-stop]')) button.disabled = true;
    }
    $('fault').textContent = s.fault || '控制正常；加湿停止后至少等待 5 秒再启动。';
    $('fanMode').textContent = !s.enabled ? '系统已停止，风机不输出' : {schedule: '按周期自动通风', on: '手动开启，30 秒后恢复定时', off: '手动保持关闭'}[s.fan_mode];
    if (s.climate?.ventilation) $('fanMode').textContent = 'CO2超限，强制通风中';
    $('humidityHint').textContent = '≤ ' + s.config.humidity_low + '% 启动 / ≥ ' + s.config.humidity_high + '% 停止';
    for (const button of document.querySelectorAll('#start,#stop,#save')) button.disabled = controlBusy;
    $('start').disabled = controlBusy || s.enabled;
    for (const button of document.querySelectorAll('[data-fan]')) button.disabled = controlBusy || !s.enabled;
    if (displayedBoot !== s.boot_id) { configLoaded = false; displayedBoot = s.boot_id; }
    if (!configLoaded) { for (const [key, val] of Object.entries(s.config)) $('settings').elements[key].value = val; configLoaded = true; }
  }
  $('lastUpdate').textContent = value.connected ? '最近接收：' + value.age_s.toFixed(1) + ' 秒前' : '当前没有有效连接';
  $('events').replaceChildren(...value.events.map(event => {
    const li = document.createElement('li'), time = document.createElement('time'), text = document.createElement('span');
    time.dateTime = event.at; time.textContent = dateText(event.at); text.textContent = event.text;
    li.append(time, text); return li;
  }));
}
async function refresh() {
  if (polling) return;
  polling = true;
  try { render(await api('/api/status')); } catch (error) { markOffline('后台未连接，无法确认输出状态；开发板通信超时后停机。'); }
  finally { polling = false; }
}
async function history() {
  try { rows = (await api('/api/history')).rows; draw(); } catch (_) { $('chartSummary').textContent = '历史数据读取失败'; }
}
function draw() {
  const canvas = $('chart'), rect = canvas.getBoundingClientRect(), scale = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * scale); canvas.height = Math.round(rect.height * scale);
  const ctx = canvas.getContext('2d'); ctx.scale(scale, scale);
  const width = rect.width, height = rect.height, left = 33, right = width - 12, top = 10, bottom = height - 24;
  ctx.font = '10px system-ui'; ctx.fillStyle = '#909b86';
  const minimum = Math.min(0, ...rows.filter(r => r.sensor_ok).map(r => Math.floor(r.temperature / 20) * 20));
  const maximum = 100, extent = maximum - minimum;
  for (let tick = minimum; tick <= maximum; tick += 20) {
    const y = bottom - (bottom - top) * (tick - minimum) / extent;
    ctx.beginPath(); ctx.strokeStyle = '#e9eddf'; ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke(); ctx.fillText(tick, 4, y + 3);
  }
  if (!rows.length) { ctx.fillText('等待第一条采集记录', left + 15, 90); return; }
  const first = Date.parse(rows[0].at), last = Date.parse(rows[rows.length - 1].at), span = Math.max(last - first, 1000);
  for (const [field, color] of [['humidity', '#427b50'], ['temperature', '#ba8750']]) {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2; let previous = null;
    for (const row of rows) {
      const time = Date.parse(row.at), value = row[field];
      if (!row.sensor_ok || value === null) { previous = null; continue; }
      const x = left + (time - first) / span * (right - left), y = bottom - (value - minimum) / extent * (bottom - top);
      if (previous === null || time - previous > 4000) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      previous = time;
    }
    ctx.stroke();
  }
  ctx.fillStyle = '#909b86'; ctx.fillText(new Date(first).toLocaleTimeString('zh-CN', {hour12: false}), left, height - 3);
  ctx.textAlign = 'right'; ctx.fillText(new Date(last).toLocaleTimeString('zh-CN', {hour12: false}), right, height - 3);
  $('chartSummary').textContent = '已显示 ' + rows.length + ' 条记录。温度与湿度共用数值刻度，单位见图例；完整记录可导出。';
}
async function command(action, values) {
  if (controlBusy) return;
  controlBusy = true;
  if (latestStatus) render(latestStatus);
  show('正在等待控制应答…');
  try { const result = await api('/api/control', {action, values}); show(result.message); await refresh(); await history(); }
  catch (error) { show(error.message, true); }
  finally { controlBusy = false; if (latestStatus) render(latestStatus); }
}
$('start').onclick = () => command('start'); $('stop').onclick = () => command('stop');
for (const button of document.querySelectorAll('[data-fan]')) button.onclick = () => command('fan', button.dataset.fan);
for (const button of document.querySelectorAll('[data-liquid]')) button.onclick = () => {
  const pump = button.dataset.liquid, input = $(pump + 'Seconds');
  if (input.reportValidity()) command('liquid', {pump, seconds: Number(input.value)});
};
for (const button of document.querySelectorAll('[data-liquid-stop]')) button.onclick = () => command('liquid', {pump: button.dataset.liquidStop, seconds: 0});
$('settings').onsubmit = async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target).entries());
  for (const key of Object.keys(values)) values[key] = Number(values[key]);
  await command('config', values);
};
for (const button of document.querySelectorAll('[data-scenario]')) button.onclick = async () => {
  try { await api('/api/scenario', {name: button.dataset.scenario}); await refresh(); await history(); show('演示场景已更新'); }
  catch (error) { show(error.message, true); }
};
$('reportButton').onclick = async () => {
  try { $('report').textContent = (await api('/api/report')).text; } catch (error) { show(error.message, true); }
};
$('aiButton').onclick = async () => {
  $('aiButton').disabled = true; $('aiButton').textContent = '本机分析中…';
  try { const result = await api('/api/analysis', {}); $('report').textContent = '本地模型建议 · ' + result.model + '\n仅供人工核查\n\n' + result.text + '\n\n—— 对应规则统计证据 ——\n' + result.evidence.text; }
  catch (error) { show('模型分析失败：' + error.message + '。规则统计仍可使用。', true); }
  finally { $('aiButton').disabled = false; $('aiButton').textContent = '本地模型建议'; }
};
async function photos() {
  try {
    const result = await api('/api/photos');
    refreshPhotoChoices(result.photos);
    if (!result.photos.length) return;
    $('photos').replaceChildren(...result.photos.map(photo => {
      const link = document.createElement('a'), image = document.createElement('img'), caption = document.createElement('span');
      link.className = 'photo-card'; link.href = photo.url; link.target = '_blank'; link.rel = 'noopener';
      image.src = photo.url; image.alt = '生长记录 ' + dateText(photo.at); image.loading = 'lazy'; caption.textContent = dateText(photo.at);
      link.append(image, caption); return link;
    }));
  } catch (error) { show('照片列表读取失败', true); }
}
function stopCamera() {
  clearInterval(photoTimer); photoTimer = null; $('autoPhoto').checked = false;
  if (stream) { stream.getTracks().forEach(track => track.stop()); stream = null; }
  $('video').srcObject = null; $('video').hidden = true; $('cameraPlaceholder').hidden = false;
  $('capture').disabled = $('cameraStop').disabled = $('autoPhoto').disabled = true;
  $('cameraStart').disabled = false;
}
async function startCamera() {
  if (stream?.active) return true;
  $('cameraStart').disabled = true;
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw new Error('当前浏览器不支持摄像头，请使用本机 Edge 或 Chrome');
    stream = await navigator.mediaDevices.getUserMedia({video: {width: {ideal: 960}, height: {ideal: 540}}, audio: false});
    $('video').srcObject = stream; await $('video').play();
    stream.getVideoTracks()[0].onended = () => { stopCamera(); show('摄像头已断开', true); };
    $('video').hidden = false; $('cameraPlaceholder').hidden = true;
    $('capture').disabled = $('cameraStop').disabled = $('autoPhoto').disabled = false;
    show('摄像头已开启');
    return true;
  } catch (error) { stopCamera(); show('摄像头开启失败：' + error.message, true); return false; }
}
$('cameraStart').onclick = startCamera;
let capturing = false;
async function capture() {
  if (capturing) return false;
  capturing = true;
  try {
    if (!stream || !$('video').videoWidth || !stream.getVideoTracks()[0]?.enabled) throw new Error('摄像头尚未就绪');
    const canvas = document.createElement('canvas'), video = $('video');
    const ratio = Math.min(1, 1280 / video.videoWidth, 720 / video.videoHeight);
    canvas.width = Math.floor(video.videoWidth * ratio); canvas.height = Math.floor(video.videoHeight * ratio);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
    if (!blob) throw new Error('图像生成失败');
    await api('/api/photos', blob, 'image/png'); await photos(); show('照片已保存到本机');
    return true;
  } catch (error) { show('拍照失败：' + error.message, true); return false; }
  finally { capturing = false; }
}
$('capture').onclick = capture; $('cameraStop').onclick = stopCamera;
$('autoPhoto').onchange = () => {
  clearInterval(photoTimer); photoTimer = null;
  if ($('autoPhoto').checked) { photoTimer = setInterval(capture, 300000); show('已开启定时拍照，首张将在 5 分钟后保存'); }
};
window.addEventListener('beforeunload', stopCamera);
new ResizeObserver(draw).observe($('chart'));
window.addEventListener('DOMContentLoaded', () => {
  refresh(); history(); photos(); setInterval(refresh, 1000); setInterval(history, 5000);
});


let agentBusy = false;
function agentMessage(role, text, result) {
  const article = document.createElement('article'), title = document.createElement('strong'), body = document.createElement('p');
  article.className = 'agent-message ' + role;
  title.textContent = role === 'user' ? '你' : '智能体';
  body.textContent = text; article.append(title, body);
  if (result) {
    const source = document.createElement('small');
    source.textContent = (result.source === 'demo' ? '模拟演示' : '实物串口') + ' · ' + (result.planner === 'guard' ? '指令检查' : result.planner || '工具执行');
    article.append(source);
    if (result.trace?.length) {
      const details = document.createElement('details'), summary = document.createElement('summary'), list = document.createElement('ol');
      summary.textContent = '查看工具调用与结果';
      for (const step of result.trace) {
        const row = document.createElement('li');
        row.textContent = (step.pending ? '待页面处理' : step.ok ? '已完成' : '未完成') + ' · ' + step.label + '\n' + step.result;
        list.append(row);
      }
      details.append(summary, list); article.append(details);
    }
    if (result.ok === false) article.classList.add('failed');
  }
  $('agentMessages').append(article);
  while ($('agentMessages').children.length > 40) $('agentMessages').firstElementChild.remove();
  $('agentMessages').scrollTop = $('agentMessages').scrollHeight;
}
async function executeCameraAction(action) {
  if (action === 'camera_analyze') return (await analyzePhoto()).text.slice(0, 490);
  if (action === 'camera_start') {
    if (!await startCamera()) throw new Error('摄像头未开启，请检查权限或设备');
    return '摄像头已开启，画面仅在当前页面预览。';
  }
  if (action === 'camera_stop') {
    stopCamera(); return '摄像头与定时拍照已关闭。';
  }
  if (action === 'camera_capture') {
    if (!await capture()) throw new Error('照片未保存，请先开启摄像头并检查画面');
    return '照片已保存到本机，可在生长影像区查看。';
  }
  if (action === 'camera_auto_off') {
    clearInterval(photoTimer); photoTimer = null; $('autoPhoto').checked = false;
    return '已关闭当前页面的定时拍照。';
  }
  if (action === 'camera_auto_on') {
    if (!stream?.active) throw new Error('请先开启摄像头');
    clearInterval(photoTimer); photoTimer = setInterval(capture, 300000); $('autoPhoto').checked = true;
    return '已开启每5分钟拍照，首张将在5分钟后保存；页面需保持打开。';
  }
  throw new Error('未知页面操作，未执行');
}
async function sendAgent(message, fastStop = false) {
  if (agentBusy && !fastStop) return;
  if (!fastStop) { agentBusy = true; $('agentSend').disabled = true; }
  const identifier = crypto.randomUUID();
  agentMessage('user', message);
  $('agentProgress').textContent = fastStop ? '正在停止…' : '正在理解指令并调用工具…';
  const startedAt = Date.now();
  const progressTimer = fastStop ? null : setInterval(() => {
    $('agentProgress').textContent = '正在处理，已等待 ' + Math.floor((Date.now() - startedAt) / 1000) + ' 秒；可随时点击全部停止。';
  }, 1000);
  try {
    let result = await api('/api/agent', {id: identifier, message});
    if (result.client_action) {
      const action = result.client_action; let ok = true, detail;
      try { detail = await executeCameraAction(action); }
      catch (error) { ok = false; detail = error.message; }
      try { result = await api('/api/agent/client-result', {id: identifier, action, ok, detail}); }
      catch (error) { result = {...result, ok: false, text: detail + '\n页面结果未能写入后台：' + error.message}; }
    }
    agentMessage('assistant', result.text, result);
    if (result.trace?.some(step => step.tool === 'update_settings' && step.ok)) configLoaded = false;
    if (result.trace?.some(step => step.tool === 'update_climate' && step.ok)) climateLoaded = false;
    await refresh(); await history(); await photos();
  } catch (error) {
    agentMessage('assistant', '请求未完成：' + error.message + '。执行结果可能未知，请查看状态和操作记录；不会自动重复发送。');
  } finally {
    clearInterval(progressTimer);
    if (!fastStop) { agentBusy = false; $('agentSend').disabled = false; }
    $('agentProgress').textContent = agentBusy ? '正在等待上一条指令结束…' : '';
  }
}
$('agentForm').onsubmit = event => {
  event.preventDefault();
  if (agentBusy) return;
  const message = $('agentInput').value.trim();
  if (message) { $('agentInput').value = ''; sendAgent(message); }
};
$('agentInput').onkeydown = event => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    event.preventDefault();
    if (!agentBusy) $('agentForm').requestSubmit();
  }
};
$('agentStop').onclick = () => sendAgent('全部停止', true);
for (const button of document.querySelectorAll('[data-prompt]')) button.onclick = () => {
  $('agentInput').value = button.dataset.prompt; $('agentInput').focus();
};

