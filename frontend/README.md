# 影片推荐智能体 · 前端 (frontend/)

极简前端：Vue 3 + Vite + TypeScript。**不引入** Pinia / vue-router / 任何 UI 组件库；
状态用轻量 `reactive` 组合式 store，视图切换靠条件渲染，样式为纯 CSS。

## 开发
```bash
npm install
npm run dev        # http://localhost:5173 （与后端 CORS 白名单一致）
```
后端 API 地址通过环境变量 `VITE_API_BASE` 注入，默认 `http://localhost:8000`。

## 构建
```bash
npm run build      # 产物在 dist/，可静态托管
npm run preview
```

## 目录
```
frontend/
├─ index.html
├─ vite.config.ts
├─ tsconfig.json / tsconfig.node.json
└─ src/
   ├─ main.ts
   ├─ App.vue                # 按登录态切换 Login / Home
   ├─ styles.css             # 极简全局样式
   ├─ api/client.ts          # 原生 fetch 封装（Bearer / 错误归一）
   ├─ store/auth.ts          # 轻量鉴权 store（register/login/me/logout + localStorage）
   └─ views/
      ├─ Login.vue           # 注册/登录切换
      └─ Home.vue            # 登录后壳层（M2 占位，后续接多会话/对话）
```

## 与后端对接
- 登录拿 token → 存 `localStorage['ma_token']` → 后续请求自动带 `Authorization: Bearer <token>`。
- 受保护接口：`/auth/me`、`/sessions/*`、`/memory/*`、`/recommend`、`/recommend/stream` 等。
- 启用后端鉴权：改后端 `.env` 的 `AUTH_ENABLED=true` 并设 `AUTH_SECRET`。
