'use strict';
const $ = id => document.getElementById(id);
let images = [];
let busy = false;
function showError(message) { $('error').textContent = message; $('error').hidden = !message; }
function updateCount() { $('char-count').textContent = `${$('context').value.length} / 20000`; }
$('context').addEventListener('input', updateCount);
document.querySelectorAll('[data-intent]').forEach(button => button.addEventListener('click', () => { $('intent').value = button.dataset.intent; }));
$('paste').addEventListener('click', async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text.length > 20000) throw new Error('聊天文字超过 20000 字，请只粘贴最近的上下文。');
    $('context').value = text; updateCount(); showError('');
  } catch (error) { showError(error.message.includes('20000') ? error.message : '请长按聊天内容输入框，选择“粘贴”。剪贴板按钮需要 HTTPS 和系统授权。'); $('context').focus(); }
});
function renderImages() {
  $('image-list').replaceChildren();
  images.forEach((src, i) => {
    const box = document.createElement('div'); box.className = 'image-item';
    const img = document.createElement('img'); img.src = src; img.alt = `聊天截图 ${i + 1}`;
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', `移除截图 ${i + 1}`);
    remove.addEventListener('click', () => { images.splice(i, 1); renderImages(); });
    box.append(img, remove); $('image-list').append(box);
  });
}
async function readImage(file) {
  if (!file.type.startsWith('image/')) throw new Error('请选择图片文件。');
  if (file.size > 20 * 1024 * 1024) throw new Error('单张图片不能超过 20 MB。');
  const bitmap = await new Promise((resolve, reject) => {
    const image = new Image(); const url = URL.createObjectURL(file);
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('图片无法读取，请尝试将截图转为 JPEG 或 PNG。')); };
    image.src = url;
  });
  const scale = Math.min(1, 2000 / Math.max(bitmap.naturalWidth, bitmap.naturalHeight));
  const canvas = document.createElement('canvas'); canvas.width = Math.round(bitmap.naturalWidth * scale); canvas.height = Math.round(bitmap.naturalHeight * scale);
  const ctx = canvas.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  const data = canvas.toDataURL('image/jpeg', 0.87);
  if (data.length > 4 * 1024 * 1024) throw new Error('截图压缩后仍然过大，请裁剪后重试。');
  return data;
}
$('images').addEventListener('change', async event => {
  const files = Array.from(event.target.files); event.target.value = '';
  if (files.length + images.length > 3) { showError('最多添加 3 张截图，请先移除不需要的图片。'); return; }
  $('generate').disabled = true;
  try { const added = await Promise.all(files.map(readImage)); images.push(...added); renderImages(); showError(''); }
  catch (error) { showError(error.message); }
  finally { $('generate').disabled = busy; }
});
function renderReplies(replies) {
  $('reply-list').replaceChildren();
  replies.forEach((reply, i) => {
    const card = document.createElement('article'); card.className = 'reply';
    const top = document.createElement('div'); top.className = 'reply-top';
    const label = document.createElement('span'); label.textContent = `${String(i + 1).padStart(2, '0')} / ${reply.label}`;
    const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'copy'; copy.textContent = '复制回复';
    const text = document.createElement('textarea'); text.value = reply.text; text.rows = Math.min(8, Math.max(3, Math.ceil(reply.text.length / 18))); text.setAttribute('aria-label', `编辑第 ${i + 1} 条回复`);
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(text.value); copy.textContent = '已复制 ✓'; setTimeout(() => { copy.textContent = '复制回复'; }, 1800); }
      catch { text.focus(); text.select(); text.setSelectionRange(0, text.value.length); showError('请长按选中的回复，选择“复制”。'); }
    });
    top.append(label, copy); card.append(top, text); $('reply-list').append(card);
  });
  $('results').hidden = false;
  $('results').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
}
async function generate(event) {
  event?.preventDefault();
  if (busy) return;
  if (!$('context').value.trim() && !images.length) { showError('先粘贴聊天内容，或添加一张聊天截图。'); $('context').focus(); return; }
  busy = true; showError(''); $('generate').disabled = true; $('again').disabled = true; $('generate').textContent = '正在想合适的回复…';
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 70000);
  try {
    const response = await fetch('/api/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ context: $('context').value, intent: $('intent').value, relationship: $('relationship').value, tone: $('tone').value, images }), signal: controller.signal });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '生成失败，请稍后重试。');
    renderReplies(result.replies);
  } catch (error) { showError(error.name === 'AbortError' ? '生成超时，请稍后重试。' : error.message || '网络连接失败，请检查服务是否运行。'); }
  finally { clearTimeout(timer); busy = false; $('generate').disabled = false; $('again').disabled = false; $('generate').textContent = '帮我想回复 ↗'; }
}
$('reply-form').addEventListener('submit', generate); $('again').addEventListener('click', generate);
$('clear').addEventListener('click', () => { if (busy) return; $('context').value = ''; $('intent').value = ''; $('relationship').value = ''; images = []; renderImages(); updateCount(); $('reply-list').replaceChildren(); $('results').hidden = true; showError(''); $('context').focus(); });
fetch('/api/status').then(response => response.json()).then(status => {
  $('service-status').textContent = status.configured ? 'AI 服务已配置 · 内容仅在点击生成时提交' : '界面已就绪 · 尚需在服务端配置 AI 密钥，才能生成回复';
  $('service-status').classList.toggle('pending', !status.configured);
}).catch(() => { $('service-status').textContent = '无法连接服务，请检查服务是否运行。'; $('service-status').classList.add('pending'); });
// No localStorage, analytics, external fonts, or service-worker chat caches.
