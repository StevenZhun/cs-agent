import { createApp } from 'vue'
import {
  Alert,
  Avatar,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Divider,
  Drawer,
  Dropdown,
  Empty,
  Input,
  Layout,
  List,
  Modal,
  Select,
  Pagination,
  Popconfirm,
  Result,
  Spin,
  Tabs,
  Tag,
  Textarea,
} from '@arco-design/web-vue'
import '@arco-design/web-vue/es/alert/style/css.js'
import '@arco-design/web-vue/es/avatar/style/css.js'
import '@arco-design/web-vue/es/button/style/css.js'
import '@arco-design/web-vue/es/card/style/css.js'
import '@arco-design/web-vue/es/checkbox/style/css.js'
import '@arco-design/web-vue/es/select/style/css.js'
import '@arco-design/web-vue/es/modal/style/css.js'
import '@arco-design/web-vue/es/descriptions/style/css.js'
import '@arco-design/web-vue/es/divider/style/css.js'
import '@arco-design/web-vue/es/drawer/style/css.js'
import '@arco-design/web-vue/es/dropdown/style/css.js'
import '@arco-design/web-vue/es/empty/style/css.js'
import '@arco-design/web-vue/es/input/style/css.js'
import '@arco-design/web-vue/es/layout/style/css.js'
import '@arco-design/web-vue/es/list/style/css.js'
import '@arco-design/web-vue/es/pagination/style/css.js'
import '@arco-design/web-vue/es/popconfirm/style/css.js'
import '@arco-design/web-vue/es/result/style/css.js'
import '@arco-design/web-vue/es/spin/style/css.js'
import '@arco-design/web-vue/es/tabs/style/css.js'
import '@arco-design/web-vue/es/tag/style/css.js'
import '@arco-design/web-vue/es/textarea/style/css.js'
import App from './App.vue'
import './style.css'

const app = createApp(App)
for (const component of [
  Alert,
  Avatar,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Divider,
  Drawer,
  Dropdown,
  Empty,
  Input,
  Layout,
  List,
  Modal,
  Select,
  Pagination,
  Popconfirm,
  Result,
  Spin,
  Tabs,
  Tag,
  Textarea,
]) {
  app.use(component)
}
app.mount('#app')
