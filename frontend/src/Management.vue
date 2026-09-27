<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
const emit = defineEmits(['new', 'connect', 'empty', 'updated'])
const tunnels = ref([])
const busy = ref(false)
const error = ref('')
const editItem = ref(null)
const deleteItem = ref(null)
const form = ref({ name: '', prefix: '', port: '', host_mode: 'auto' })
let timer
const online = computed(() => tunnels.value.filter(item => item.running).length)
const valid = computed(() => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(form.value.prefix) && Number(form.value.port) >= 1 && Number(form.value.port) <= 65535)
async function api(path, options = {}) {
  const response = await fetch('/api' + path, { ...options, headers: { 'Content-Type': 'application/json' } })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '操作失败')
  return data
}
async function refresh() {
  tunnels.value = await api('/tunnels')
  emit('updated', tunnels.value)
  if (!tunnels.value.length) emit('empty')
}
async function action(fn) {
  busy.value = true; error.value = ''
  try { await fn() } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function toggle(item) { await action(async () => { await api(`/tunnels/${item.id}/${item.running ? 'stop' : 'start'}`, { method: 'POST' }); await refresh() }) }
function openEdit(item) {
  editItem.value = item
  form.value = { name: item.name, prefix: item.hostname.slice(0, -(item.zone_name.length + 1)), port: item.port, host_mode: item.host_mode || 'auto' }
  error.value = ''
}
async function save() {
  if (!valid.value || !editItem.value) return
  await action(async () => {
    await api(`/tunnels/${editItem.value.id}`, { method: 'PUT', body: JSON.stringify({ ...form.value, port: Number(form.value.port) }) })
    editItem.value = null
    await refresh()
  })
}
async function remove() {
  if (!deleteItem.value) return
  await action(async () => {
    await api(`/tunnels/${deleteItem.value.id}`, { method: 'DELETE' })
    deleteItem.value = null
    await refresh()
  })
}
async function copy(value) {
  try { await navigator.clipboard.writeText(value) } catch { error.value = '复制失败，请手动复制地址。' }
}
onMounted(() => { refresh().catch(e => { error.value = e.message }); timer = setInterval(() => { if (!busy.value) refresh().catch(() => {}) }, 5000) })
onUnmounted(() => clearInterval(timer))
</script>
<template>
  <div class="management">
    <div class="management-intro"><div><div class="eyebrow">YOUR TUNNELS · ALL IN ONE PLACE</div><h1>服务管理<span class="headline-period">.</span></h1><p>管理公开地址，调整端口，或继续添加服务。</p></div><button class="primary" @click="emit('new')">新建穿透 <span>＋</span></button></div>
    <div v-if="error && !editItem && !deleteItem" class="alert">{{ error }} <button @click="error = ''" aria-label="关闭提示">×</button></div>
    <div class="overview"><div><span>已创建服务</span><strong>{{ tunnels.length }}</strong></div><div><span>在线连接</span><strong>{{ online }}</strong></div><div class="account-overview"><span>账户与域名</span><strong>{{ [...new Set(tunnels.map(item => item.zone_name))].join(' · ') }}</strong><button @click="emit('connect')">连接其他域名 ↗</button></div></div>
    <div class="management-heading"><h2>所有服务</h2><button class="subtle" @click="refresh">刷新状态</button></div>
    <div class="tunnel-grid"><article v-for="item in tunnels" :key="item.id" class="tunnel-card"><div class="card-top"><span class="tunnel-icon">↗</span><span class="badge" :class="{ running: item.running }">{{ item.running ? '运行中' : item.enabled ? '连接异常' : '已暂停' }}</span></div><h3>{{ item.name }}</h3><a :href="item.url" target="_blank" rel="noopener">{{ item.url }} ↗</a><div class="card-details"><span>本地端口 <b>:{{ item.port }} · {{ item.local_reachable ? '可连接' : '不可达' }}</b></span><span>Cloudflare 域名 <b>{{ item.zone_name }}</b></span></div><p v-if="item.error" class="card-error">{{ item.error }}</p><div class="card-actions"><button @click="copy(item.url)">复制地址</button><button :disabled="busy" @click="toggle(item)">{{ item.running ? '暂停' : item.enabled ? '重试' : '启动' }}</button><button @click="openEdit(item)">编辑</button><button class="danger-text" @click="deleteItem = item">删除</button></div></article></div>
    <p class="management-hint">公开地址可被互联网访问。请为应用本身设置登录或访问保护。</p>
    <div v-if="editItem" class="modal-backdrop" @click.self="editItem = null"><section class="modal" role="dialog" aria-modal="true" aria-label="编辑服务"><div class="modal-head"><div><div class="panel-kicker">EDIT SERVICE</div><h2>编辑服务</h2></div><button class="close" @click="editItem = null">×</button></div><p class="muted">更新地址或端口后，连接会自动重启。</p><label for="edit-name">服务名称</label><input id="edit-name" v-model.trim="form.name" maxlength="80" /><label for="edit-prefix">地址前缀</label><div class="host-input"><input id="edit-prefix" v-model.trim="form.prefix" /><span>.{{ editItem.zone_name }}</span></div><label for="edit-port">本地端口</label><input id="edit-port" v-model="form.port" type="number" min="1" max="65535" /><label for="edit-host-mode">应用地址兼容</label><select id="edit-host-mode" v-model="form.host_mode"><option value="auto">自动检测（推荐）</option><option value="public">保留公开域名</option><option value="local">使用 localhost</option></select><p class="form-help">如果应用只接受 localhost，自动检测会调整转发请求。</p><div v-if="error" class="alert">{{ error }}</div><div class="actions"><button class="subtle" @click="editItem = null">取消</button><button class="primary" :disabled="busy || !valid" @click="save">保存更改</button></div></section></div>
    <div v-if="deleteItem" class="modal-backdrop" @click.self="deleteItem = null"><section class="modal" role="alertdialog" aria-modal="true" aria-label="删除服务"><div class="modal-head"><div><div class="panel-kicker">REMOVE SERVICE</div><h2>删除这个服务？</h2></div><button class="close" @click="deleteItem = null">×</button></div><p class="muted">将删除 {{ deleteItem.url }} 的 DNS 记录和 Cloudflare Tunnel。</p><div v-if="error" class="alert">{{ error }}</div><div class="actions"><button class="subtle" @click="deleteItem = null">取消</button><button class="danger-button" :disabled="busy" @click="remove">确认删除</button></div></section></div>
  </div>
</template>
