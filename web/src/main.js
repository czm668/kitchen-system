import { createApp } from 'vue'
import App from './App.vue'
import './styles/theme.css'

// 挂载前先把主题定下来：等到 onMounted 才设会让浅色主题闪一下深色底色
const saved = localStorage.getItem('kitchen.theme')
if (saved && saved !== 'auto') {
  document.documentElement.setAttribute('data-theme', saved)
}

createApp(App).mount('#app')
