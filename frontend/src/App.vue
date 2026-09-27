<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Management from './Management.vue'

const step = ref(0)
const view = ref('guide')
const tunnels = ref([])
const serviceName = ref('')
const createdUrl = ref('')
const status = ref({})
const services = ref([])
const loginId = ref('')
const zones = ref([])
const zoneId = ref('')
const prefix = ref('app')
const port = ref('')
const hostMode = ref('auto')
const busy = ref(false)
const error = ref('')
let timer
let loginPopup
const loginUrl = ref('')
function popupFeatures() {
  const width = 700
  const height = 640
  const left = Math.max(0, Math.round(window.screenX + (window.outerWidth - width) / 2))
  const top = Math.max(0, Math.round(window.screenY + (window.outerHeight - height) / 2))
  return `popup=yes,width=${width},height=${height},left=${left},top=${top}`
}
function railActive(index) { return [step.value <= 1, step.value === 2, step.value === 3, step.value === 4][index] }
function railPassed(index) { return [step.value >= 2, step.value >= 3, step.value >= 4, false][index] }
function goToStep(index) {
  const target = [0, 2, 3, 4][index]
  if (target < step.value || (target === 4 && step.value === 4)) step.value = target
}
const zone = computed(() => zones.value.find(item => item.id === zoneId.value))
const selected = computed(() => services.value.find(item => item.port === Number(port.value)))
const canCreate = computed(() => zone.value && /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(prefix.value) && Number(port.value) >= 1 && Number(port.value) <= 65535)

async function api(path, options = {}) {
  const response = await fetch('/api' + path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } })
  const value = await response.json()
  if (!response.ok) throw new Error(value.detail || '操作失败')
  return value
}
async function run(task) {
  busy.value = true; error.value = ''
  try { await task() } catch (e) { error.value = e.message } finally { busy.value = false }
}
async function refresh() {
  const [nextStatus, nextTunnels] = await Promise.all([api('/status'), api('/tunnels')])
  status.value = nextStatus
  tunnels.value = nextTunnels
}
async function loadZones() {
  zones.value = await api('/zones')
  if (!zones.value.some(item => item.id === zoneId.value)) zoneId.value = zones.value[0]?.id || ''
}
function newTunnel() {
  view.value = 'guide'
  step.value = status.value.account_connected ? 2 : 0
  port.value = ''
  hostMode.value = 'auto'
  prefix.value = 'app'
  serviceName.value = ''
  error.value = ''
  if (status.value.account_connected) loadZones().catch(e => { error.value = e.message })
  scan().catch(e => { error.value = e.message })
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
function onTunnelsUpdated(items) { tunnels.value = items }
async function handleEmpty() { await refresh(); newTunnel() }
function openManage() {
  view.value = 'manage'
  refresh().catch(e => { error.value = e.message })
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
function connectAnother() { view.value = 'guide'; step.value = 0; window.scrollTo({ top: 0, behavior: 'smooth' }) }
async function scan() { await run(async () => { services.value = await api('/services') }) }
async function begin() {
  if (busy.value) return
  // Create the popup during the click event; browsers block windows opened
  // after awaiting a network request.
  if (loginPopup && !loginPopup.closed) loginPopup.close()
  loginId.value = ''
  loginPopup = window.open('', 'autotunnel-cloudflare-login', popupFeatures())
  if (loginPopup) {
    try {
      loginPopup.document.title = 'Cloudflare 登录'
      loginPopup.document.body.innerHTML = '<p style="font:16px sans-serif;padding:32px">正在准备 Cloudflare 登录…</p>'
    } catch { /* An existing cross-origin popup may be closing. */ }
  }
  await run(async () => {
    const value = await api('/login', { method: 'POST' })
    loginId.value = value.login_id
    loginUrl.value = value.authorization_url
    if (loginPopup && !loginPopup.closed) loginPopup.location.replace(loginUrl.value)
    else error.value = '浏览器阻止了登录弹窗，请允许弹窗后点击下方按钮。'
    step.value = 1
    if (timer) clearInterval(timer)
    timer = setInterval(checkLogin, 1500)
  })
  if (error.value && !loginId.value && loginPopup && !loginPopup.closed) loginPopup.close()
}
function reopenLogin() {
  if (!loginUrl.value) return
  loginPopup = window.open(loginUrl.value, 'autotunnel-cloudflare-login', popupFeatures())
  if (!loginPopup) error.value = '浏览器阻止了登录弹窗，请允许此站点打开弹窗。'
}
async function checkLogin() {
  if (!loginId.value || busy.value) return
  try {
    const loginState = await api('/login/' + loginId.value)
    if (!loginState.authorized) return
    clearInterval(timer); timer = null
    if (loginPopup && !loginPopup.closed) loginPopup.close()
    await refresh()
    await loadZones()
    zoneId.value = loginState.zone_id || zones.value[0]?.id || ''
    step.value = 2
  } catch (e) { error.value = e.message; clearInterval(timer); timer = null }
}
async function create() {
  if (!canCreate.value) return
  await run(async () => {
    const item = await api('/tunnels', { method: 'POST', body: JSON.stringify({ zone_id: zoneId.value, prefix: prefix.value, port: Number(port.value), name: serviceName.value, host_mode: hostMode.value }) })
    createdUrl.value = item.url
    await refresh()
    step.value = 4
  })
}
async function copy() { await navigator.clipboard.writeText(createdUrl.value) }
onMounted(async () => {
  try {
    await refresh()
    if (status.value.account_connected) { await loadZones(); step.value = 2 }
    await scan()
    if (tunnels.value.length) view.value = 'manage'
  } catch (e) { error.value = e.message }
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="shell">
    <header class="topbar"><div class="brand"><span class="brandmark">↗</span> AutoTunnel</div><nav v-if="tunnels.length" class="main-nav" aria-label="主导航"><button :class="{ selected: view === 'manage' }" @click="openManage">服务管理</button><button :class="{ selected: view === 'guide' }" @click="newTunnel">新建穿透</button></nav><span v-else class="topnote">本地服务，一步上线</span><span class="state"><span class="dot" :class="{ online: tunnels.some(item => item.running) }"></span>{{ tunnels.filter(item => item.running).length ? `${tunnels.filter(item => item.running).length} 个服务在线` : '本机运行' }}</span></header>
    <main>
      <Management v-if="view === 'manage'" @new="newTunnel" @connect="connectAnother" @empty="handleEmpty" @updated="onTunnelsUpdated" />
      <template v-else>
      <div class="intro"><div class="eyebrow">CLOUDFLARE TUNNEL · MADE SIMPLE</div><h1>让本地应用，<br><em>轻松上线。</em></h1><p>{{ status.account_connected ? '选择服务和域名，创建一个新地址。' : '登录 Cloudflare，选择服务和域名，即可上线。' }}</p></div>
      <div class="layout">
        <aside class="rail"><div class="rail-title">快速开始</div><button v-for="(label, index) in ['连接账户', '选择本地服务', '设置公开地址', '完成']" :key="label" class="rail-step" :class="{ active: railActive(index), passed: railPassed(index) }" @click="goToStep(index)"><span class="step-number">{{ railPassed(index) ? '✓' : String(index + 1).padStart(2, '0') }}</span><span>{{ label }}</span></button><div class="rail-help"><b>无需改路由器设置</b><p>AutoTunnel 会配置 Cloudflare Tunnel 和 DNS，让外部请求安全地抵达本机服务。</p></div></aside>
        <section class="panel">
          <div v-if="error" class="alert">{{ error }} <button @click="error = ''" aria-label="关闭提示">×</button></div>
          <template v-if="step === 0"><div class="panel-kicker">01 / 连接账户</div><h2>先连接 Cloudflare</h2><p class="muted">在弹窗中完成授权。凭据会保存在本机数据库，下次无需重新登录。</p><div class="feature"><span class="feature-icon">☁</span><div><strong>使用你的 Cloudflare 域名</strong><small>登录后可选择授权域名，生成固定的 HTTPS 地址。</small></div></div><button class="primary" :disabled="busy || (!status.cloudflared_available && !status.cloudflared_installable)" @click="begin">{{ busy ? '正在准备登录…' : '登录 Cloudflare' }} <span>↗</span></button><p v-if="!status.cloudflared_available" class="hint">首次使用会自动下载并校验 cloudflared，请稍候。</p></template>
          <template v-else-if="step === 1"><div class="panel-kicker">01 / 连接账户</div><h2>完成浏览器中的登录</h2><p class="muted">请在弹窗中完成授权。完成后这里会自动继续。</p><div class="waiting"><span class="spinner"></span><div><strong>正在等待授权</strong><small>请在 Cloudflare 弹窗中选择域名。</small></div></div><div class="actions" style="justify-content:flex-start"><button class="subtle" @click="reopenLogin">打开登录弹窗</button><button class="subtle" @click="checkLogin">检查授权状态</button><button class="subtle" :disabled="busy" @click="begin">重新登录</button></div></template>
          <template v-else-if="step === 2"><div class="panel-kicker">02 / 选择本地服务</div><h2>你要公开哪个应用？</h2><p class="muted">我们找到了本机监听端口和 Docker 映射端口。也可以直接输入端口。</p><div class="service-list"><button v-for="item in services" :key="item.port" class="service" :class="{ chosen: Number(port) === item.port }" @click="port = item.port; if (!serviceName) serviceName = item.name"><span class="service-icon">{{ item.source === 'Docker' ? '◇' : '⌘' }}</span><span class="service-name"><strong>{{ item.name }}</strong><small>{{ item.source }} · {{ item.detail }}</small></span><code>:{{ item.port }}</code></button><div v-if="!services.length" class="empty">暂未发现服务。输入应用正在监听的端口即可继续。</div></div><div class="manual"><label for="port">或者手动输入端口</label><input id="port" v-model="port" type="number" min="1" max="65535" placeholder="例如 3000" /></div><div class="actions"><button class="subtle" :disabled="busy" @click="scan">重新扫描</button><button class="primary" :disabled="!Number(port)" @click="step = 3">下一步 <span>→</span></button></div></template>
          <template v-else-if="step === 3"><div class="panel-kicker">03 / 设置公开地址</div><h2>为它起一个好记的地址</h2><p class="muted">选择 Cloudflare 域名，再填写前缀。我们会创建 Tunnel 和 DNS 记录。</p><div class="form"><label for="service-name">服务名称</label><input id="service-name" v-model.trim="serviceName" class="form-input" maxlength="80" placeholder="例如：我的相册" /><label for="zone">Cloudflare 域名</label><select id="zone" v-model="zoneId"><option v-for="item in zones" :key="item.id" :value="item.id">{{ item.name }}</option></select><button class="inline-link" @click="connectAnother">连接其他域名 ↗</button><label for="prefix">地址前缀</label><div class="host-input"><input id="prefix" v-model.trim="prefix" placeholder="app" /><span>.{{ zone?.name || 'example.com' }}</span></div><label for="host-mode">应用地址兼容</label><select id="host-mode" v-model="hostMode"><option value="auto">自动检测（推荐）</option><option value="public">保留公开域名</option><option value="local">使用 localhost</option></select><p class="form-help">如果应用只接受 localhost（例如 Vite 预览服务），自动检测会调整转发请求。</p><div class="preview"><span>即将创建</span><strong>https://{{ prefix || 'app' }}.{{ zone?.name || 'example.com' }}</strong><small>→ http://127.0.0.1:{{ port }}</small></div></div><div class="actions"><button class="subtle" @click="step = 2">返回</button><button class="primary" :disabled="busy || !canCreate" @click="create">{{ busy ? '正在创建…' : '创建并连接' }} <span>→</span></button></div></template>
          <template v-else><div class="panel-kicker">04 / 已完成</div><h2>新服务已添加。</h2><p class="muted">你可以在服务管理中调整地址、端口，或继续添加服务。</p><div class="result"><span>公开地址</span><a :href="createdUrl" target="_blank" rel="noopener">{{ createdUrl }} ↗</a><small>本机端口 {{ port }}</small></div><div class="actions"><button class="subtle" @click="newTunnel">继续添加</button><button class="primary" @click="openManage">查看服务管理 <span>→</span></button></div><p class="hint">公开的应用可能被互联网上任何人访问。请确认应用本身已设置登录或访问保护。</p></template>
        </section>
      </div>
      </template>
    </main><footer>AutoTunnel <span>由 Cloudflare Tunnel 提供连接能力</span></footer>
  </div>
</template>
