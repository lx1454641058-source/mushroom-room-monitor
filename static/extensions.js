'use strict';
let climateBoot = null, climateLoaded = false, visionBusy = false, lastVisionRecord = null;
function renderClimate(c, boot) {
  for (const id of ['climateEnable', 'climateDisable', 'climateSave']) $(id).disabled = !c || controlBusy;
  if (!c) {
    $('climateState').textContent = '温控与CO2状态未知；可能未连接或旧固件不支持';
    $('climateFault').textContent = '未获得有效状态，不能确认输出。'; return;
  }
  $('climateState').textContent = 'CO2：' + (c.co2_ok ? c.co2 + ' ppm' : '未知/故障') + ' · 加热：' + (c.heater ? '开启' : '关闭') + ' · 制冷：' + (c.cooler ? '开启' : '关闭') + ' · 联动：' + (c.enabled ? '启用' : '关闭');
  $('climateFault').textContent = c.fault;
  $('climateEnable').disabled = controlBusy || c.enabled || !latestStatus?.status.enabled || !c.co2_ok;
  $('climateDisable').disabled = controlBusy || !c.enabled;
  $('climateSave').disabled = controlBusy || c.enabled;
  if (climateBoot !== boot) { climateLoaded = false; climateBoot = boot; }
  if (!climateLoaded) {
    for (const [key, value] of Object.entries(c.config)) $('climateSettings').elements[key].value = value;
    climateLoaded = true;
  }
}
$('climateEnable').onclick = () => command('climate', {operation: 'enable'});
$('climateDisable').onclick = () => command('climate', {operation: 'disable'});
$('climateSettings').onsubmit = event => {
  event.preventDefault();
  const config = Object.fromEntries([...new FormData(event.target)].map(([key, value]) => [key, Number(value)]));
  command('climate', {operation: 'config', config});
};
function refreshPhotoChoices(items) {
  const selected = $('visionPhoto').value;
  $('visionPhoto').replaceChildren(...items.map(photo => {
    const option = document.createElement('option'); option.value = photo.id;
    option.textContent = dateText(photo.at) + ' · ' + photo.id.slice(0, 8); return option;
  }));
  if (items.some(p => p.id === selected)) $('visionPhoto').value = selected;
}
async function analyzePhoto() {
  if (visionBusy) throw new Error('视觉分析正在运行，请等待');
  const photo_id = $('visionPhoto').value, species = $('visionSpecies').value.trim();
  if (!photo_id || !species) throw new Error('先保存照片并填写实际菌种');
  visionBusy = true; $('visionAnalyze').disabled = true; $('visionDownload').disabled = true;
  lastVisionRecord = null; $('visionResult').textContent = '本机视觉模型分析中，最多等待约两分钟…';
  try {
    const result = await api('/api/vision', {photo_id, species});
    lastVisionRecord = result; $('visionDownload').disabled = false;
    $('visionResult').textContent = '照片：' + result.photo_id + '\n菌种（用户填写）：' + result.species_declared + '\n模型：' + result.model + '\n\n' + result.text;
    return result;
  } catch (error) { $('visionResult').textContent = '未完成识别：' + error.message; throw error; }
  finally { visionBusy = false; $('visionAnalyze').disabled = false; }
}
$('visionAnalyze').onclick = () => analyzePhoto().catch(error => show(error.message, true));
$('visionDownload').onclick = () => {
  if (!lastVisionRecord) return;
  const link = document.createElement('a');
  const url = URL.createObjectURL(new Blob([JSON.stringify(lastVisionRecord, null, 2)], {type: 'application/json'}));
  link.href = url; link.download = 'vision-' + lastVisionRecord.id + '.json'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('visionUpload').onchange = async event => {
  const file = event.target.files[0]; if (!file) return;
  try {
    if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 10 * 1024 * 1024) throw new Error('请选择10MB以内PNG或JPEG');
    const bitmap = await createImageBitmap(file);
    try {
      const ratio = Math.min(1, 1280 / bitmap.width, 720 / bitmap.height);
      const canvas = document.createElement('canvas'); canvas.width = Math.max(1, Math.floor(bitmap.width * ratio)); canvas.height = Math.max(1, Math.floor(bitmap.height * ratio));
      canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error('图片转换失败');
      const result = await api('/api/photos', blob, 'image/png'); await photos(); $('visionPhoto').value = result.id;
      show('照片已导入本机；填写菌种后分析');
    } finally { bitmap.close(); }
  } catch (error) { show(error.message, true); }
  finally { event.target.value = ''; }
};
for (const [name, label] of [['temperature_low','温度降低'], ['temperature_high','温度升高'], ['co2_high','CO2升高'], ['co2_normal','CO2恢复'], ['co2_fault','CO2传感器故障']]) {
  const button = document.createElement('button'); button.textContent = label;
  button.onclick = async () => { try { await api('/api/scenario', {name}); await refresh(); } catch(error) { show(error.message, true); } };
  document.querySelector('.scenario-buttons').append(button);
}
for (const text of ['启用温控与二氧化碳联动', '关闭温控与二氧化碳联动', '识别选定照片']) {
  const button = document.createElement('button'); button.textContent = text;
  button.onclick = () => { $('agentInput').value = text; $('agentInput').focus(); };
  document.querySelector('.agent-examples').append(button);
}
